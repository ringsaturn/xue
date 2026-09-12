import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import {
  GeoJSONSource,
  Map as MaplibreMap,
  NavigationControl,
  Popup,
  setWorkerUrl,
  type MapOptions,
} from "maplibre-gl";
// maplibre-gl 6 resolves its worker from `import.meta.url`, which points at
// the bundle rather than the package once Vite has processed it. `?worker&url`
// emits a self-contained worker chunk (the dist worker imports a sibling
// module, so a plain `?url` copy would fail on its first import).
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

import { CRC32_INITIAL, crc32Hex, crc32Update } from "./crc32";
import { fetchImmutable } from "./fetchimmutable";
import {
  applyStaticMessages,
  basemapLang,
  htmlLang,
  locale,
  localeHtmlLang,
  LOCALES,
  onLocaleChange,
  setLocale,
  t,
  type Locale,
  type MessageKey,
} from "./i18n";
import { createSheet, fillLanguageList } from "./sheet";
import { ForecastLayer, MAX_NAMED_CONTOURS, type ContourStyle, type VectorField } from "./layer";
import {
  KNOWN_BUNDLE_IDS,
  FORECAST_MODEL_IDS,
  FORECAST_MODELS,
  fetchLatestPointer,
  fetchManifest,
  hasBundle,
  isBundleVariableId,
  parseBundleMetadata,
  pickBundleVariant,
  axisUnitSeconds,
  frameOffsets,
  HOUR_SECONDS,
  isObservationModel,
  sameTimeAxis,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  type ForecastModelId,
  type KnownBundleId,
  type VideoBundleDescriptor,
} from "./manifest";
import {
  identifyBundle,
  identityForBundleId,
  registeredBundleId,
  sameIdentity,
  type VariableIdentity,
} from "./identity";
import {
  FAMILIES,
  ISOBARIC_FILL_IDS,
  bundleLevel,
  familyLabel,
  familyMembers,
  familyOf,
  isobaricCode,
  isobaricLegend,
  levelCode,
  niceStep,
  rangeLegend,
  scalarLegendRange,
  vectorMaxMagnitude,
  type IsobaricFamily,
} from "./levels";
import { buildPalette, buildVapourFluxPalette, buildWindFieldPalette, legendGradient } from "./palettes";
import {
  PRESSURE_BUNDLE_IDS,
  PRESSURE_LEVELS,
  isPressureBundle,
  pressureCode,
  pressureLabel,
  pressureLegend,
  pressureLevelForIdentity,
  type PressureBundleId,
} from "./pressure";
import {
  DEFAULT_FPS,
  defaultFpsForLoop,
  frameDwellScales,
  loopDwellUnits,
  nextFps,
  parseStoredFps,
  type PlaybackFps,
} from "./playback";
import {
  DEFAULT_VARIABLE,
  parseCaseFromSearch,
  parseLinesFromSearch,
  parseModelFromSearch,
  parseParticlesFromSearch,
  parseResolutionFromSearch,
  parseUseH264FromSearch,
  parseVariableFromSearch,
  searchForCaseVariable,
  searchForVariable,
  searchWithLines,
  searchWithParticles,
} from "./urlstate";
import { WindParticleLayer } from "./particles";
import type { Feature, FeatureCollection } from "geojson";
import { strideFor, type CellWindow, type LabelRequest } from "./isolines";
import type { LabelsWorkerRequest, LabelsWorkerResponse } from "./labels.worker";
import {
  ProbeSeries,
  geoGrid,
  probeSeriesValues,
  probeWindDirection,
  wrap,
  type ProbeValue,
  type ProbeVariable,
} from "./probe";
import { fetchPoster, isPosterSupported } from "./poster";
import { frameCacheKey, parseFrameCacheKey, variableKey } from "./sessionkeys";
import { applyTheme, isDark, onThemeChange, toggleTheme } from "./theme";
import {
  coverageBox,
  coversTiles,
  parseTileGeometry,
  sameTileRects,
  tileCount,
  viewportTileRects,
  WHOLE_PLANE_COVERAGE,
  type CoverageBox,
  type TileGeometry,
  type TileRect,
} from "./tiles";
import { applyPageMeta } from "./pagemeta";
import {
  caseCameraLimits,
  fetchCaseManifest,
  fetchCatalog,
  localizedText,
  type ShowcaseCase,
} from "./showcase-catalog";
import {
  createVideoDecodeChannel,
  isWebCodecsSupported,
  type DecodeChannel,
  type VideoFrameIndexEntry,
  type VideoStreamSource,
} from "./webcodecs";

// Rewrite the static shell into the detected locale and appearance before
// anything renders.
applyStaticMessages();
applyTheme();

/** Playback pacing (the ladder and the per-frame dwell live in playback.ts).
 * The rate is user-adjustable from the transport's speed button because a
 * dataset's loop length varies by two orders of magnitude: a 240-hour run is
 * 161 frames, a showcase case can be a couple of dozen. */
let playbackFps: PlaybackFps = DEFAULT_FPS;
/** Wall-clock time one shortest-step frame holds. */
let frameIntervalMs = 1000 / playbackFps;
/** The hold in force for the frame on screen — the interval scaled by that
 * frame's own forecast step. The blend sweep divides by it, so it is tracked
 * rather than recomputed. */
let currentHoldMs = frameIntervalMs;
/** Set once the viewer picks a speed: from then on their choice outranks the
 * per-dataset default, here and on later visits. */
let playbackFpsChosen = false;
const PLAYBACK_FPS_KEY = "xue-playback-fps";
/** Pre-manifest placeholder frame count: the GFS 240-hour axis
 * (121 hourly frames, then 40 three-hourly). */
const FRAME_COUNT = 161;
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
  // The window is runway in time, not in frames: faster playback burns
  // through the same number of frames sooner, so it needs proportionally
  // more of them resident ahead of the playhead.
  const base = constrainedConnection() ? 4 : 10;
  return Math.max(3, Math.round((base * playbackFps) / DEFAULT_FPS));
}
function prefetchConcurrency(): number {
  return constrainedConnection() ? 1 : 3;
}

/** Per-variable UI copy; the `code` is prefixed with the active model's label
 * ("GFS / TMP 2M", "ECMWF / TMP 2M"). */
interface VariableUi {
  code: string;
  title: readonly string[];
  bufferTitle: string;
  label: string;
  legend: readonly string[];
}

/** The pressure family's nine entries, built from the level registry rather
 * than written out: they differ only in the level, and the instrument panel's
 * code and legend are already derived there. */
function pressureVariableUi(): Record<PressureBundleId, VariableUi> {
  const entries = {} as Record<PressureBundleId, VariableUi>;
  for (const id of PRESSURE_BUNDLE_IDS) {
    const level = PRESSURE_LEVELS[id];
    entries[id] = {
      code: pressureCode(id),
      title: level.levelHpa === null ? ["Sea Level", "Pressure"] : [`${level.levelHpa} hPa`, "Height"],
      bufferTitle: level.levelHpa === null ? "Pressure buffer" : "Height buffer",
      label: pressureLabel(id),
      legend: pressureLegend(id),
    };
  }
  return entries;
}

/** The upper-air fills, built from the family registry the same way: the
 * code, title and legend all follow from the family and the level. */
function isobaricVariableUi(): Record<string, VariableUi> {
  const words: Record<IsobaricFamily, string> = {
    hgt: "Height",
    tmp: "Temperature",
    rh: "Humidity",
    spfh: "Specific Humidity",
    wind: "Wind",
    qflux: "Vapour Flux",
    vvel: "Vertical Velocity",
    thetae: "Theta-e",
    cloud: "Cloud Cover",
  };
  const entries: Record<string, VariableUi> = {};
  for (const id of ISOBARIC_FILL_IDS) {
    const word = words[familyOf(id)!];
    entries[id] = {
      code: isobaricCode(id),
      title: [`${bundleLevel(id)} hPa`, word],
      bufferTitle: `${word} buffer`,
      label: familyLabel(id),
      legend: isobaricLegend(identityForBundleId(id)!) ?? [],
    };
  }
  return entries;
}

/** The whole table, built rather than written so that the translated
 * labels in it follow a language switch: `VARIABLE_UI` is rebuilt from
 * here on every `onLocaleChange`. */
function buildVariableUi(): Record<string, VariableUi> {
  return {
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
    cref: {
      code: "CREF EATM",
      title: ["Composite", "Reflectivity"],
      bufferTitle: "Reflectivity buffer",
      label: t("varLabelCref"),
      legend: ["75", "60", "45", "30", "15", "0"],
    },
    wind10m: {
      code: "WIND 10M",
      title: ["Surface", "Wind"],
      bufferTitle: "Wind buffer",
      label: t("varLabelWind10m"),
      legend: ["40", "30", "20", "10", "5", "0"],
    },
    // The surface diagnostics' legends read off their chart ceilings
    // (levels.ts), the way the upper-air fills' do.
    gust: {
      code: "GUST SFC",
      title: ["Wind", "Gust"],
      bufferTitle: "Gust buffer",
      label: t("varLabelGust"),
      legend: isobaricLegend(identityForBundleId("gust")!) ?? [],
    },
    tcdc: {
      code: "TCDC EATM",
      title: ["Cloud", "Cover"],
      bufferTitle: "Cloud buffer",
      label: t("varLabelTcdc"),
      legend: isobaricLegend(identityForBundleId("tcdc")!) ?? [],
    },
    cape: {
      code: "CAPE SFC",
      title: ["Convective", "Energy"],
      bufferTitle: "CAPE buffer",
      label: t("varLabelCape"),
      legend: isobaricLegend(identityForBundleId("cape")!) ?? [],
    },
    lcdc: {
      code: "LCDC LOW",
      title: ["Low", "Cloud"],
      bufferTitle: "Cloud buffer",
      label: t("varLabelLcdc"),
      legend: isobaricLegend(identityForBundleId("lcdc")!) ?? [],
    },
    mcdc: {
      code: "MCDC MID",
      title: ["Middle", "Cloud"],
      bufferTitle: "Cloud buffer",
      label: t("varLabelMcdc"),
      legend: isobaricLegend(identityForBundleId("mcdc")!) ?? [],
    },
    hcdc: {
      code: "HCDC HIGH",
      title: ["High", "Cloud"],
      bufferTitle: "Cloud buffer",
      label: t("varLabelHcdc"),
      legend: isobaricLegend(identityForBundleId("hcdc")!) ?? [],
    },
    vis: {
      code: "VIS SFC",
      title: ["Surface", "Visibility"],
      bufferTitle: "Visibility buffer",
      label: t("varLabelVis"),
      legend: isobaricLegend(identityForBundleId("vis")!) ?? [],
    },
    dpt2m: {
      code: "DPT 2M",
      title: ["Dew", "Point"],
      bufferTitle: "Dew point buffer",
      label: t("varLabelDpt2m"),
      legend: isobaricLegend(identityForBundleId("dpt2m")!) ?? [],
    },
    aptmp2m: {
      code: "APTMP 2M",
      title: ["Apparent", "Temperature"],
      bufferTitle: "Apparent temperature buffer",
      label: t("varLabelAptmp2m"),
      legend: isobaricLegend(identityForBundleId("aptmp2m")!) ?? [],
    },
    ...pressureVariableUi(),
    ...isobaricVariableUi(),
  } as unknown as Record<string, VariableUi>;
}

let VARIABLE_UI: Record<string, VariableUi> = buildVariableUi();

interface BasemapTones {
  ocean: string;
  land: string;
  /** Painted behind everything — visible past the coastlines' antialiasing
   * and across the whole canvas until the first tiles arrive. Defaults to
   * `ocean`, which is what a themed slate wants; a layer that leaves the
   * flavor alone names the flavor's own background instead. */
  background?: string;
}

function pressureBasemapTheme(tones: BasemapTones): Record<PressureBundleId, BasemapTones> {
  const themes = {} as Record<PressureBundleId, BasemapTones>;
  for (const id of PRESSURE_BUNDLE_IDS) themes[id] = tones;
  return themes;
}

/** The upper-air fills take the ground of the surface field they read like:
 * the opaque temperatures the 2 m temperature's, the winds the 10 m wind's,
 * and the translucent moisture fields (relative and specific humidity, vapour
 * flux) the precipitation's slate. */
function isobaricBasemapTheme(pick: (family: IsobaricFamily) => BasemapTones): Record<string, BasemapTones> {
  const themes: Record<string, BasemapTones> = {};
  for (const id of ISOBARIC_FILL_IDS) themes[id] = pick(familyOf(id)!);
  return themes;
}

/** Basemap tones per variable, per theme. tmp2m paints an opaque field so its
 * base is nearly invisible; prate and wind composite semi-transparent data
 * over the base, so those get a base with enough contrast of its own to keep
 * the page from reading as a flat void (dark) or a blank sheet (light).
 *
 * The pressure family is drawn as thin lines over a nearly bare map, which is
 * what a chart looks like: the base has to carry the geography on its own, so
 * it is the plainest of the set — on paper, the design's own chart stock. */
const DARK_BASEMAP: Record<string, BasemapTones> = {
  tmp2m: { ocean: "#0b1826", land: "#182c3d" },
  prate: { ocean: "#16344a", land: "#28495f" },
  dswrf: { ocean: "#0d1b2b", land: "#1c3242" },
  // Radar echoes are small and bright; the base stays dark enough for a
  // 5 dBZ edge to read against it.
  cref: { ocean: "#0c1a26", land: "#1a2f3d" },
  wind10m: { ocean: "#0e2131", land: "#1d3849" },
  // Translucent at the low end like the wind field, and on its ground.
  gust: { ocean: "#0e2131", land: "#1d3849" },
  // A grey veil over a slate: clear sky has to read as the map, so the
  // ground carries the geography and stays dark enough for thin cloud to
  // show against it.
  tcdc: { ocean: "#16344a", land: "#28495f" },
  // Stable air is the map; the ramp starts in pale straw, which needs the
  // precipitation slate under it rather than the temperature's near-void.
  cape: { ocean: "#16344a", land: "#28495f" },
  lcdc: { ocean: "#16344a", land: "#28495f" },
  mcdc: { ocean: "#16344a", land: "#28495f" },
  hcdc: { ocean: "#16344a", land: "#28495f" },
  // Clear air is the map, and the reduced-visibility ramp comes in
  // translucent: the same slate.
  vis: { ocean: "#16344a", land: "#28495f" },
  // Opaque fields on the temperature's near-void.
  dpt2m: { ocean: "#0b1826", land: "#182c3d" },
  aptmp2m: { ocean: "#0b1826", land: "#182c3d" },
  ...pressureBasemapTheme({ ocean: "#101f2c", land: "#22384a" }),
  // Relative humidity is a light wash, not a coat, and goes with the
  // moisture fields on the precipitation slate rather than with the
  // temperature's near-void; θe is a coat like the temperature.
  ...isobaricBasemapTheme((family) =>
    family === "wind"
      ? { ocean: "#0e2131", land: "#1d3849" }
      : family === "tmp" || family === "thetae"
        ? { ocean: "#0b1826", land: "#182c3d" }
        : { ocean: "#16344a", land: "#28495f" },
  ),
} as Record<string, BasemapTones>;

/** Protomaps' data-viz flavor for the light theme: white land, pale grey
 * water, and no landcover tinting the continents — a base that stays out of
 * the data's way. The dark theme keeps the "dark" flavor. */
const LIGHT_FLAVOR = "white";

/** The white flavor's own ground, read from the flavor rather than copied
 * into hexes here: a light-theme layer that takes these tones leaves the
 * basemap exactly as Protomaps drew it. */
const PAPER_GROUND: BasemapTones = {
  ocean: namedFlavor(LIGHT_FLAVOR).water,
  land: namedFlavor(LIGHT_FLAVOR).earth,
  background: namedFlavor(LIGHT_FLAVOR).background,
};

/** Light means a light map: every layer sits on that same white ground, the
 * four whose palettes run translucent at the low end included, so the page
 * is one sheet rather than paper chrome floating over a dark map. Drizzle,
 * the faintest wind and a 5 dBZ edge read weaker on white than they do on
 * the dark theme's slate — the accepted cost of one palette serving both
 * themes (see PRECIPITATION_STOPS in palettes.ts).
 *
 * The pressure family keeps its chart stock. Contours are a weather chart,
 * and the warm sheet is the design's own paper for one. */
const LIGHT_BASEMAP: Record<string, BasemapTones> = {
  tmp2m: PAPER_GROUND,
  prate: PAPER_GROUND,
  dswrf: PAPER_GROUND,
  cref: PAPER_GROUND,
  wind10m: PAPER_GROUND,
  gust: PAPER_GROUND,
  tcdc: PAPER_GROUND,
  lcdc: PAPER_GROUND,
  mcdc: PAPER_GROUND,
  hcdc: PAPER_GROUND,
  cape: PAPER_GROUND,
  vis: PAPER_GROUND,
  dpt2m: PAPER_GROUND,
  aptmp2m: PAPER_GROUND,
  ...pressureBasemapTheme({ ocean: "#dcd6c8", land: "#c9c2b2" }),
  ...isobaricBasemapTheme(() => PAPER_GROUND),
} as Record<string, BasemapTones>;

/** Read at every use rather than once: the theme toggles in place. */
function basemapThemes(): Record<string, BasemapTones> {
  return isDark ? DARK_BASEMAP : LIGHT_BASEMAP;
}

function currentBasemapTheme(): BasemapTones {
  const themes = basemapThemes();
  const id = document.body.dataset.variable;
  return themes[id ?? "tmp2m"] ?? themes.tmp2m!;
}

/** Relative luminance of a `#rrggbb` tone. */
function luminance(color: string): number {
  const value = Number.parseInt(color.slice(1), 16);
  return (0.2126 * ((value >> 16) & 255) + 0.7152 * ((value >> 8) & 255) + 0.0722 * (value & 255)) / 255;
}

function applyBasemapTheme(): void {
  const theme = currentBasemapTheme();
  // Everything drawn over the data — the title, the color scale's numbers,
  // the credits, and the basemap's own place labels and boundaries — takes
  // its ink from the ground it sits on rather than from the theme. Since the
  // light theme went white every one of its grounds is light and this is
  // constant there, but the dark theme still has grounds of its own and one
  // rule covering both is what keeps the two from drifting.
  const darkGround = luminance(theme.ocean) < 0.5;
  document.body.dataset.ground = darkGround ? "dark" : "light";
  if (map.getLayer("background"))
    map.setPaintProperty("background", "background-color", theme.background ?? theme.ocean);
  if (map.getLayer("water")) map.setPaintProperty("water", "fill-color", theme.ocean);
  if (map.getLayer("earth")) map.setPaintProperty("earth", "fill-color", theme.land);
  applyBasemapInk(darkGround);
}

/** Repaint the basemap's labels and boundaries for the current ground.
 *
 * A session switches layers under those labels, and the ground changes with
 * the layer while the flavor's own text colors do not, so the two colors
 * that have to stay legible are set here instead of coming from the flavor
 * (and set again after `syncBasemapStyle` has repainted the flavor). */
function applyBasemapInk(darkGround: boolean): void {
  // The style's layers exist once it has loaded; `isStyleLoaded()` would also
  // wait out every tile in flight and skip the repaint after a pan.
  if (!mapStyleReady) return;
  const ink = darkGround ? "#eef1f4" : "#3a3730";
  const halo = darkGround ? "rgba(0, 0, 0, 0.75)" : "rgba(243, 239, 230, 0.92)";
  const border = darkGround ? "rgba(255, 255, 255, 0.32)" : "rgba(27, 26, 23, 0.3)";
  for (const layer of map.getStyle().layers ?? []) {
    if (layer.type === "symbol") {
      map.setPaintProperty(layer.id, "text-color", ink);
      map.setPaintProperty(layer.id, "text-halo-color", halo);
    } else if (layer.id === "boundaries" || layer.id === "boundaries_country") {
      map.setPaintProperty(layer.id, "line-color", border);
    }
  }
  applyLabelInk();
}

/** Protomaps hosted basemap (real coastlines, waterways, boundaries and
 * labels). The forecast layers insert themselves before this layer, keeping
 * boundaries and place labels legible above the data. */
const FORECAST_ANCHOR_LAYER = "boundaries_country";
const PROTOMAPS_KEY = "249bb192fefe0a77";

// maplibre-gl 6 no longer re-exports the style-spec types; take the style
// object's type from the map options that consume it.
type BasemapStyle = Exclude<MapOptions["style"], string | undefined>;

function buildBasemapStyle(): BasemapStyle {
  const theme = currentBasemapTheme();
  const flavorName = isDark ? "dark" : LIGHT_FLAVOR;
  const flavor = {
    ...namedFlavor(flavorName),
    background: theme.background ?? theme.ocean,
    water: theme.ocean,
    earth: theme.land,
  };
  return {
    version: 8,
    glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
    sprite: `https://protomaps.github.io/basemaps-assets/sprites/v4/${flavorName}`,
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

setWorkerUrl(maplibreWorkerUrl);

/** The basemap style the map currently carries, as built — what
 * `syncBasemapStyle` diffs the next build against. */
let appliedBasemapStyle: BasemapStyle = buildBasemapStyle();

const map = new MaplibreMap({
  container: "map",
  center: [128, 28],
  zoom: 1.65,
  minZoom: 0,
  maxZoom: 7,
  attributionControl: false,
  style: appliedBasemapStyle,
});
map.addControl(new NavigationControl({ showCompass: false }), "top-right");

type StyleProperties = Record<string, unknown> | undefined;
type PaintName = Parameters<typeof map.setPaintProperty>[1];
type PaintValue = Parameters<typeof map.setPaintProperty>[2];
type LayoutName = Parameters<typeof map.setLayoutProperty>[1];
type LayoutValue = Parameters<typeof map.setLayoutProperty>[2];

/** Apply every property of `next` that differs from `before`, and reset the
 * ones `next` no longer has. */
function diffStyleProperties(
  before: StyleProperties,
  next: StyleProperties,
  set: (name: string, value: unknown) => void,
): void {
  for (const name of new Set([...Object.keys(before ?? {}), ...Object.keys(next ?? {})])) {
    const previous = before?.[name];
    const value = next?.[name];
    if (JSON.stringify(previous) !== JSON.stringify(value)) set(name, value);
  }
}

/** Bring the basemap onto the current theme and locale without rebuilding
 * it. Both flavors and all ten label languages come out of the same
 * `basemapLayers` call — the same layers under the same ids, differing only
 * in their paint and layout values — so a switch is the difference between
 * the style the map carries and a fresh build, applied property by property,
 * plus the flavor's sprite. `map.setStyle` would do the same and also drop
 * the custom WebGL layers the forecast is drawn in, which is what this
 * exists to avoid. The ground tones and the ink go on top afterwards,
 * through `applyBasemapTheme`, as they do on every layer switch. */
function syncBasemapStyle(): void {
  if (!mapStyleReady) return;
  const next = buildBasemapStyle();
  if (next.sprite !== appliedBasemapStyle.sprite && typeof next.sprite === "string") map.setSprite(next.sprite);
  const previous = new Map(appliedBasemapStyle.layers.map((layer) => [layer.id, layer]));
  for (const layer of next.layers) {
    const before = previous.get(layer.id);
    if (!before || !map.getLayer(layer.id)) continue;
    const id = layer.id;
    const paintBefore = (before as { paint?: StyleProperties }).paint;
    const paintNext = (layer as { paint?: StyleProperties }).paint;
    const layoutBefore = (before as { layout?: StyleProperties }).layout;
    const layoutNext = (layer as { layout?: StyleProperties }).layout;
    // The names come from the style itself; the typed overloads only know
    // the property names written out in the spec.
    diffStyleProperties(paintBefore, paintNext, (name, value) =>
      map.setPaintProperty(id, name as PaintName, value as PaintValue),
    );
    diffStyleProperties(layoutBefore, layoutNext, (name, value) =>
      map.setLayoutProperty(id, name as LayoutName, value as LayoutValue),
    );
    const filterBefore = (before as { filter?: unknown }).filter;
    const filterNext = (layer as { filter?: unknown }).filter;
    if (JSON.stringify(filterBefore) !== JSON.stringify(filterNext)) {
      map.setFilter(id, filterNext as Parameters<typeof map.setFilter>[1]);
    }
  }
  appliedBasemapStyle = next;
  applyBasemapTheme();
}

const slider = required<HTMLInputElement>("frame-slider");
const runTime = required<HTMLElement>("run-time");
const forecastLead = required<HTMLOutputElement>("forecast-hour");
const leadLabel = required<HTMLElement>("lead-label");
const runTimeLabel = required<HTMLElement>("run-time-label");
const validTimeLabel = required<HTMLElement>("valid-time-label");
const loadStatus = required<HTMLElement>("load-status");
const tickMarks = required<HTMLElement>("tick-marks");
const errorPanel = required<HTMLElement>("error-panel");
const errorMessage = required<HTMLElement>("error-message");
const retryButton = required<HTMLButtonElement>("retry-button");
const playButton = required<HTMLButtonElement>("play-button");
const playLabel = required<HTMLElement>("play-label");
const speedButton = required<HTMLButtonElement>("speed-button");
const particlesToggle = required<HTMLButtonElement>("particles-toggle");
const speedLabel = required<HTMLElement>("speed-label");
const validTime = required<HTMLElement>("valid-time");
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
const legendBar = legend.querySelector<HTMLElement>(".legend-bar")!;
const legendUnit = required<HTMLElement>("legend-unit");
const legendLabels = required<HTMLElement>("legend-labels");
const trackHorizon = required<HTMLElement>("track-horizon");
const frameTooltip = required<HTMLOutputElement>("frame-tooltip");
const forecastDays = required<HTMLElement>("forecast-days");
const timelinePanel = required<HTMLElement>("timeline-panel");
const levelRow = required<HTMLElement>("level-row");
const modelTrigger = required<HTMLButtonElement>("model-trigger");
const modelSheet = required<HTMLElement>("model-sheet");
const creditsTrigger = required<HTMLButtonElement>("credits-trigger");
const creditsSheet = required<HTMLElement>("credits-sheet");
const langTrigger = required<HTMLButtonElement>("lang-toggle");
const langSheet = required<HTMLElement>("lang-sheet");
// Scoped to buttons: <body> carries data-variable/data-model too (styling
// state), and must never be hidden or aria-pressed like a switch button.
const variableRail = document.querySelector<HTMLElement>(".variable-rail");
/** The layer rail's tiles. Read live rather than snapshotted: the shell
 * writes one tile per family it knows, and a run publishing a bundle this
 * build has never heard of gets a generic tile appended here
 * (`syncUnknownRailTiles`) — a layer nobody can reach is the same as one
 * nobody can see. */
function variableButtons(): HTMLButtonElement[] {
  return variableRail ? [...variableRail.querySelectorAll<HTMLButtonElement>("button[data-variable]")] : [];
}
const modelButtons = [...document.querySelectorAll<HTMLButtonElement>("button[data-model]")];
const modelEyebrow = required<HTMLElement>("model-eyebrow");
const caseBanner = required<HTMLElement>("case-banner");
const caseTitle = required<HTMLElement>("case-title");
const caseSummary = required<HTMLElement>("case-summary");
const caseRegion = required<HTMLElement>("case-region");

const MODEL_EYEBROW: Record<ForecastModelId, string> = {
  gfs: "NOAA / GFS (0.25°)",
  ecmwf: "ECMWF / IFS (0.25°)",
  sflux: "NOAA / GFS SFLUX (13 KM)",
  radar: "CMA / RADAR MOSAIC (L3 MST)",
};

/** The member to open when a family's one rail tile is picked: whichever
 * level was last on screen, else the first the run publishes — the surface
 * member where the family has one (2 m temperature, 10 m wind, sea level
 * pressure), the lowest isobaric surface otherwise. */
const lastFamilyMember = new Map<IsobaricFamily, ForecastBundleId>();

function preferredFamilyMember(family: IsobaricFamily): ForecastBundleId {
  const available = familyMembers(family).filter((id) => !manifest || hasBundle(manifest, id));
  const last = lastFamilyMember.get(family);
  if (last && available.includes(last)) return last;
  return available[0] ?? familyMembers(family)[0]!;
}

function preferredPressureVariable(): PressureBundleId {
  return preferredFamilyMember("hgt") as PressureBundleId;
}

/** The model sheet: a panel under the title on desktop, a bottom sheet on
 * phones. A case is one fixed run, so it never opens there. */
const modelSheetControl = createSheet({
  trigger: modelTrigger,
  sheet: modelSheet,
  canOpen: () => !activeCase && !switchingVariable,
  initialFocus: (sheet) => sheet.querySelector<HTMLButtonElement>('button[aria-pressed="true"]'),
});

/** The sources sheet: the credit line's contents, one row per source, opened
 * from the trigger that stands in for the line where it does not fit. */
createSheet({ trigger: creditsTrigger, sheet: creditsSheet });

/** The language picker: the same sheet again, under the round trigger in the
 * top-right column. Ten languages do not cycle on a press, so the button
 * opens a list of endonyms instead of naming the next one. */
const langSheetControl = createSheet({
  trigger: langTrigger,
  sheet: langSheet,
  initialFocus: (sheet) => sheet.querySelector<HTMLButtonElement>("button[aria-current]"),
});
/** The picker's rows, with the language in force checked; rebuilt after a
 * switch so the check moves. */
function renderLanguageList(): void {
  fillLanguageList(required<HTMLElement>("lang-list"), LOCALES, {
    current: locale,
    htmlLang: localeHtmlLang,
    onPick: (next: Locale) => {
      langSheetControl.close();
      setLocale(next);
    },
  });
}
renderLanguageList();

interface DecodedFrame {
  plane: Uint8Array;
  decodeMs: number;
  /** The tiles this plane actually holds, or null for a whole plane. Outside
   * them the buffer carries whatever the decoder held before, so a plane is
   * only reusable for a view its tiles still cover. */
  tiles: TileRect[] | null;
  /** The session that decoded it: the buffer belongs to that worker and is
   * recycled back into it, and its grid is what the plane's bytes are on.
   * A numericId alone cannot say — it is a *file-local* handle, and two open
   * bundles routinely use the same one. */
  session: VariableSession;
}

/** One resident per-variable bundle: its decode channel and embedded metadata.
 * `worker` is either a real Worker (Xue/WASM path) or a WebCodecs
 * `DecodeChannel` (video path, tmp2m only, when the browser supports it) —
 * both speak the same booted/init/ready/decode/frame/error protocol. */
interface VariableSession {
  /** Bundle-level id this session was loaded for (wind10m carries two data
   * variables). */
  id: ForecastBundleId;
  /** This session's identity in the frame cache and the probe series.
   * Variables inside a `.xue` file are numbered 1..n *per file*, so a
   * numericId is meaningful only next to the session it came from: two open
   * bundles — a temperature fill under pressure lines — both carry variable
   * 1, and every key that leaves a session has to say which. */
  key: number;
  worker: DecodeChannel;
  metadata: BundleMetadata;
  /** What this bundle is, from its variables' GRIB2 parameter blocks (or,
   * for a schemaVersion 1/2 file, from the closed legacy id list). Null when
   * the file describes something this build has no chart knowledge for — it
   * is then drawn as a plain scalar off its own codebook. */
  identity: VariableIdentity | null;
  /** The registered bundle id `identity` corresponds to: the key every chart
   * registry (palette, legend, ground tone, instrument copy) is written
   * under. Null exactly when `identity` is, or when the field sits on a
   * surface nothing is registered on. */
  chartId: KnownBundleId | null;
  /** True when the bundle carries a u/v component pair — read from the
   * parameter blocks, not from the id string. */
  vector: boolean;
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
  /** The bundle's tiling, or null when it has none — a container v1 file, or
   * the video path. Tiles are what make a viewport-sized decode and a
   * one-shot point series possible. */
  tiles: TileGeometry | null;
  /** On-demand range delivery; `bytes` grows as groups arrive. */
  streaming: boolean;
  /** Nothing left to fetch for what the viewer is looking at (immediately
   * true for full downloads). */
  resident: boolean;
  /** What `resident` covers: the whole bundle, or only the tiles the view
   * narrowed to — a narrowed session never fetches the rest, so it never
   * reaches whole-bundle residency, and the data card says which it is. */
  residentScope: "bundle" | "viewport";
  /** The tiles this session decodes for the current view, or null for the
   * whole plane. Every session on screen has its own: the filled field and
   * the lines over it come from different bundles, with different grids and
   * tilings, and each narrows to what the view needs of *its* grid. */
  viewTiles: TileRect[] | null;
  /** Lead seconds → this bundle's frame offset, built on first use. An
   * overlay is keyed off the primary session's timeline, and the two axes can
   * differ (ECMWF prate has no analysis frame), so a frame is found by lead
   * time rather than by index. */
  leadOffsets: Map<number, number> | null;
}

/** Where a session's frame offset for a lead time comes from: exact match
 * on its own axis, or null when the axis has no frame there. */
function sessionOffsetForLead(session: VariableSession, seconds: number): number | null {
  if (!session.leadOffsets) {
    const time = session.metadata.time;
    const unit = axisUnitSeconds(time);
    session.leadOffsets = new Map(frameOffsets(time).map((offset) => [offset * unit, offset]));
  }
  return session.leadOffsets.get(seconds) ?? null;
}

/** Streaming progress that arrived before its session finished registering. */
const pendingStream = new Map<
  string,
  { bytes: number; resident: boolean; scope: VariableSession["residentScope"] }
>();

let manifest: ForecastManifest | null = null;
/** Absolute URL the manifest was loaded from; artifact paths resolve against it. */
let manifestUrl: string | null = null;
/** Run id from the latest.json live pointer, e.g. "2026081600". */
let currentRun: string | null = null;
let metadata: BundleMetadata | null = null;

/** What is on screen, as slots rather than as one layer: a filled field
 * (temperature, precipitation, reflectivity, radiation, wind speed) and the
 * contour lines drawn over it (the pressure family). Either may be empty but
 * not both. Every `?type=` of old is a composition with one slot filled; the
 * pressure views are `{fill: null, lines: X}`; `?type=precip&lines=pressure`
 * fills both. Each slot's bundle is its own session — own worker, own grid,
 * own tiles, own resolution tier — and the **primary** session (the fill's,
 * or the lines' when there is no fill) drives the timeline, the legend, the
 * data card and the ground tone. The lines follow it by lead time. */
interface ViewComposition {
  fill: ForecastBundleId | null;
  lines: PressureBundleId | null;
}

/** A slot's raster layer and what it is showing. Two exist for the life of
 * the page — one per slot — and a session takes the slot its kind belongs
 * to: a pressure surface always draws in `lines`, everything else in
 * `fill`, whichever of them is primary at the time. */
interface RasterSlot {
  role: "fill" | "lines";
  layer: ForecastLayer;
  /** The session feeding this slot, or null while it is empty. */
  session: VariableSession | null;
  /** Metadata object whose grid the layer is currently configured for.
   * Sessions can legitimately differ in grid (resolution tiers), so this
   * tracks the exact metadata identity rather than a poster/full flag. */
  gridSource: BundleMetadata | null;
  /** Bundle whose REAL (bundle-decoded) frame is on screen, if any. */
  displayedReal: ForecastBundleId | null;
  /** Cache key of the plane on screen, kept so the labels can be traced
   * again and the eviction pass knows to leave it alone. */
  shownKey: string | null;
  /** Cache key of the plane an overlay is waiting on: the frame at the
   * primary's lead time, not decoded yet. The overlay keeps its last frame
   * up meanwhile and swaps when this one lands. */
  wantedKey: string | null;
}

function makeSlot(role: RasterSlot["role"]): RasterSlot {
  return {
    role,
    layer: new ForecastLayer((message) => showError(message), `forecast-${role}`),
    session: null,
    gridSource: null,
    displayedReal: null,
    shownKey: null,
    wantedKey: null,
  };
}

const slots: Record<RasterSlot["role"], RasterSlot> = { fill: makeSlot("fill"), lines: makeSlot("lines") };
let layersAdded = false;

/** The slot a bundle draws in, by its kind. */
function slotFor(id: ForecastBundleId): RasterSlot {
  return isPressureBundle(id) ? slots.lines : slots.fill;
}

/** The slot the primary session is drawing in, or null before one exists. */
function primarySlot(): RasterSlot | null {
  return activeSession ? slotFor(activeSession.id) : null;
}

/** Slots showing a session other than the primary — the overlays. */
function overlaySlots(): RasterSlot[] {
  return [slots.fill, slots.lines].filter((slot) => slot.session !== null && slot.session !== activeSession);
}

/** Every session with a slot on screen, the primary first: prefetch fans
 * out to each, and under an inflight cap the primary's requests go first. */
function slotSessions(): VariableSession[] {
  const list: VariableSession[] = [];
  if (activeSession) list.push(activeSession);
  for (const slot of overlaySlots()) list.push(slot.session!);
  return list;
}

/** Wind GPU particle layer; created alongside the scalar layer and toggled by
 * the active variable. */
let windLayer: WindParticleLayer | null = null;
let windLayerAdded = false;
let windLayerGridSource: BundleMetadata | null = null;
/** Shareable URL entry (e.g. /?model=ecmwf&type=wind) picks the initial model
 * and layer; a missing or unrecognized param falls back to the default. */
let selectedModelId: ForecastModelId = parseModelFromSearch(window.location.search);
/** Layer the URL asked for, or null when it named none — which is what lets
 * a showcase case's own default win over the app-wide one. */
const requestedVariableId: ForecastBundleId | null = parseVariableFromSearch(window.location.search);
/** Contour lines the URL asked for over the filled field, or none. */
const requestedLines: PressureBundleId | null = parseLinesFromSearch(window.location.search);
/** The primary bundle: the fill's, or the lines' when nothing is filled. */
let selectedVariableId: ForecastBundleId = requestedVariableId ?? DEFAULT_VARIABLE;
/** The composition on screen, or being switched to. */
let composition: ViewComposition = compositionForPrimary(selectedVariableId, requestedLines);

/** The composition whose primary is `primary`: a pressure surface is the
 * lines alone, anything else is the fill with `lines` kept over it. */
function compositionForPrimary(primary: ForecastBundleId, lines: PressureBundleId | null): ViewComposition {
  return isPressureBundle(primary) ? { fill: null, lines: primary } : { fill: primary, lines };
}

function compositionPrimary(view: ViewComposition): ForecastBundleId {
  return view.fill ?? view.lines ?? DEFAULT_VARIABLE;
}

/** The composition the manifest can actually serve. A slot the run does not
 * ship empties rather than errors — a showcase case carries only the layers
 * its event is about — and if that empties both, the dataset's own default
 * takes the primary slot. */
function resolveComposition(view: ViewComposition, run: ForecastManifest, fallback: ForecastBundleId): ViewComposition {
  const fill = view.fill !== null && hasBundle(run, view.fill) ? view.fill : null;
  const lines = view.lines !== null && hasBundle(run, view.lines) ? view.lines : null;
  if (fill === null && lines === null) return compositionForPrimary(fallback, null);
  return { fill, lines };
}
/** Showcase case named by `?case=<id>`: a past run cropped to one weather
 * event. A case pins its own dataset and run, so while one is open the model
 * switch is hidden, the live pointer is never read, and the new-run poll is
 * off — everything below the manifest is the ordinary viewer. */
const requestedCaseId: string | null = parseCaseFromSearch(window.location.search);
/** Whether this session may take the H.264 video path at all. It is off by
 * default — the Xue decoder is the everyday path, and the video artifacts
 * ride along only for `?use_h264=true`. */
const h264Enabled = parseUseH264FromSearch(window.location.search);
/** Resolution tier this session asks for. `auto` — the default — lets the
 * viewport and the connection pick; `?res=half` / `?res=full` pin one end of
 * the ladder, for a metered link or for a look at the full grid regardless of
 * what the view needs. */
const resolutionPreference = parseResolutionFromSearch(window.location.search);
let activeCase: ShowcaseCase | null = null;
/** A case's own default layer applies on the first load only; after that the
 * viewer keeps whatever the visitor picked, even across a retry. */
let caseDefaultApplied = false;
let activeSession: VariableSession | null = null;
let activeVariable: BundleVariable | null = null;
let activeFrameIndex: number | null = null;
/** The frame the timeline points at, which is not the one on screen while its
 * planes are still decoding. A retry aims here rather than at
 * `activeFrameIndex`: re-requesting the displayed frame would walk the readout
 * backwards under a scrub that has not landed yet. */
let requestedFrameIndex: number | null = null;
let initializeSequence = 0;
let generation = 0;
let playing = false;
let playbackFrame: number | null = null;
let nextFrameAt = 0;
let ready = false;
let switchingVariable = false;

const sessions = new Map<ForecastBundleId, VariableSession>();
const sessionLoads = new Map<ForecastBundleId, Promise<VariableSession>>();
/** Hands out `VariableSession.key`. Monotonic for the life of the page, so a
 * reloaded session never inherits a stale session's cache entries. */
let nextSessionKey = 1;

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
/** Last prefetch window sent per session, to skip redundant messages. */
const lastPrefetchWindow = new Map<ForecastBundleId, string>();
const inflight = new Map<number, string>();
let nextRequestId = 1;
let desiredKey: string | null = null;
let queuedRequest: { session: VariableSession; variable: BundleVariable; hour: number } | null = null;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

/** Where the viewer's particle-overlay choice is kept between visits, like
 * the theme, the locale and the playback rate. */
const PARTICLES_KEY = "xue-particles";

function storedParticles(): boolean | null {
  try {
    const stored = localStorage.getItem(PARTICLES_KEY);
    return stored === "1" ? true : stored === "0" ? false : null;
  } catch {
    // Storage can be unavailable (privacy modes); the default applies.
    return null;
  }
}

/** Whether the wind particles are drawn over the colored speed field. The URL
 * outranks the stored choice, which outranks the default — on, except where
 * the system asks for reduced motion: the simulation is frozen there, and now
 * that the field carries the layer on its own, a still scatter of dots is
 * worse than no overlay at all. An explicit `?particles=on` still wins. */
const requestedParticles = parseParticlesFromSearch(window.location.search);
let particlesEnabled = requestedParticles ?? storedParticles() ?? !reducedMotion.matches;
/** Whether the overlay's state is a choice — the URL's or the viewer's —
 * rather than this device's default. Only a choice is written back into the
 * address bar: a reduced-motion visitor who never touched the switch would
 * otherwise hand out links that turn the overlay off for everyone. */
let particlesChosen = requestedParticles !== null;

/** The tone the particles are drawn in over the speed field: a bright trace
 * on the dark theme, the paper theme's own ink on white. Partly transparent
 * either way — the field underneath has to read through the trails, and the
 * particles are there for direction and pace, not for a value. */
function particleInk(): readonly [number, number, number, number] {
  return isDark ? [1, 1, 1, 0.45] : [0.11, 0.1, 0.09, 0.4];
}

function required<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing element #${id}`);
  return element as T;
}

/** Status copy that stays up for a long time — the load status, the data
 * card's state line, the run stamp before a manifest, the error hint — is
 * written through here so that a language switch can say it again in the
 * new language: each element remembers the message it is showing. A stamp
 * that is not a message (a date, a copied-notice restore) goes through
 * `sayText`, which forgets the message. */
const liveCopy = new Map<HTMLElement, () => string>();

function say(element: HTMLElement, key: MessageKey, params?: Record<string, string | number>): void {
  const speak = () => t(key, params);
  // The markup's own key was the pre-JS copy; from here on the element is
  // this registry's, and the static pass must not write over it again.
  delete element.dataset.i18n;
  liveCopy.set(element, speak);
  element.textContent = speak();
}

function sayText(element: HTMLElement, text: string): void {
  delete element.dataset.i18n;
  liveCopy.delete(element);
  element.textContent = text;
}

function resayAll(): void {
  for (const [element, speak] of liveCopy) element.textContent = speak();
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

/** Short weekday in the UI locale, read in UTC like every other stamp the
 * app shows. Built per language, since the language can change under it. */
let weekdayFormat: { lang: string; format: Intl.DateTimeFormat } | null = null;

function formatDayMark(value: number): string {
  if (weekdayFormat?.lang !== htmlLang) {
    weekdayFormat = { lang: htmlLang, format: new Intl.DateTimeFormat(htmlLang, { weekday: "short", timeZone: "UTC" }) };
  }
  const date = new Date(value);
  return `${weekdayFormat.format.format(date)} ${String(date.getUTCDate()).padStart(2, "0")}`;
}

function frameCount(): number {
  return metadata?.time.frameCount ?? FRAME_COUNT;
}

// The active session's materialized frame-offset axis. Non-uniform axes
// (240-hour runs go 3-hourly past f120; the radar mosaic has gaps) make
// index<->offset conversions table lookups, so both are memoized on the
// time-block identity, along with the seconds one offset unit is worth.
let frameOffsetsSource: BundleMetadata["time"] | null = null;
let frameOffsetsCache: number[] = [];
let frameUnitSeconds = HOUR_SECONDS;
let frameScalesCache: number[] = [];
let frameIndexCache = new Map<number, number>();

function frameAxis(): number[] {
  const time = metadata?.time;
  if (!time) return [];
  if (time !== frameOffsetsSource) {
    frameOffsetsSource = time;
    frameOffsetsCache = frameOffsets(time);
    frameUnitSeconds = axisUnitSeconds(time);
    frameScalesCache = frameDwellScales(frameOffsetsCache);
    frameIndexCache = new Map(frameOffsetsCache.map((offset, index) => [offset, index]));
  }
  return frameOffsetsCache;
}

/** Wall-clock hold for the frame at `index`: the selected rate paces the
 * axis's shortest step, and a longer step holds proportionally longer so
 * forecast time keeps one apparent speed across the mixed-step tail. */
function frameHoldMs(index: number): number {
  frameAxis();
  return frameIntervalMs * (frameScalesCache[index] ?? 1);
}

/** The loop's length in frame intervals — 161 GFS frames cost 240 of them. */
function loopUnits(): number {
  frameAxis();
  return loopDwellUnits(frameScalesCache);
}

const DAY_SECONDS = 24 * HOUR_SECONDS;

/** Whether the dataset on screen is observations rather than a forecast.
 * A radar mosaic has no run cycle and no lead time: its runTime is when the
 * series starts, each frame is an observation, and the timeline reads as
 * time elapsed rather than forecast hour. */
function showingObservations(): boolean {
  return isObservationModel(activeCase ? activeCase.modelId : selectedModelId);
}

/** Retitle the timeline and station panel for the kind of dataset on screen.
 * The instrument-panel control label stays English in both locales, like
 * every other one. */
function applyDatasetWording(): void {
  const observations = showingObservations();
  leadLabel.textContent = observations ? "TIME ELAPSED" : "FORECAST HOUR";
  runTimeLabel.textContent = t(observations ? "seriesStart" : "runCycle");
  validTimeLabel.textContent = t(observations ? "observationTimeLabel" : "validTimeLabel");
  slider.setAttribute("aria-label", t(observations ? "elapsedAria" : "forecastHourAria"));
  forecastDays.setAttribute("aria-label", t(observations ? "observationDaysAria" : "forecastDaysAria"));
}

/** The frame's key on its bundle's axis — what the index and the decoders
 * address a plane by. */
function frameOffset(index: number): number {
  const offsets = frameAxis();
  return offsets.length ? (offsets[index] ?? index) : index;
}

/** Seconds from the run time to the frame at `index`. */
function frameLeadSeconds(index: number): number {
  frameAxis();
  return frameOffset(index) * frameUnitSeconds;
}

/** Frame index of an exact offset on the active axis, or -1. */
function frameIndexForOffset(offset: number): number {
  frameAxis();
  return metadata ? (frameIndexCache.get(offset) ?? -1) : offset;
}

/** The frame whose lead time is nearest to `seconds` (ties go earlier). */
function nearestFrameIndex(seconds: number): number {
  const offsets = frameAxis();
  if (offsets.length === 0) return 0;
  let best = 0;
  for (let index = 1; index < offsets.length; index += 1) {
    if (Math.abs(frameLeadSeconds(index) - seconds) < Math.abs(frameLeadSeconds(best) - seconds)) best = index;
  }
  return best;
}

function frameValidTime(index: number): number {
  const base = metadata ? Date.parse(metadata.runTime) : 0;
  return base + frameLeadSeconds(index) * 1000;
}

/** The lead-time readout. A forecast reads as the forecast hour it has
 * always been ("F058"); observations read as time elapsed from the start of
 * the series ("T+058"). A sub-hourly axis carries the minutes too
 * ("T+058:06"), because on the radar mosaic ten frames share an hour. */
function formatLead(index: number): string {
  const seconds = frameLeadSeconds(index);
  const hours = Math.floor(seconds / HOUR_SECONDS);
  const label = `${showingObservations() ? "T+" : "F"}${String(hours).padStart(3, "0")}`;
  const minutes = Math.floor((seconds % HOUR_SECONDS) / 60);
  return minutes === 0 && frameUnitSeconds >= HOUR_SECONDS
    ? label
    : `${label}:${String(minutes).padStart(2, "0")}`;
}

/** The frame cache's key. A numericId is file-local — the encoder numbers a
 * bundle's variables 1..n, so `tmp2m`'s only variable and `prmsl`'s are both
 * 1 — and the session it belongs to is what makes the pair unique
 * (sessionkeys.ts). Every reader and writer of this cache, the probe series
 * included, keys the same way. */
function cacheKey(session: VariableSession, variable: BundleVariable, offset: number): string {
  return frameCacheKey(session, variable, offset);
}

/** The probe series' key for one of a session's variables — the same session
 * scoping, without a frame offset. */
function probeKey(session: VariableSession, variable: BundleVariable): string {
  return variableKey(session, variable);
}

/** A session's variables paired with the keys their samples are filed under,
 * which is what reading a series takes. */
function probeVariables(session: VariableSession): ProbeVariable[] {
  return session.variables.map((variable) => ({ key: probeKey(session, variable), variable }));
}

function showError(message: string): void {
  stopPlayback();
  document.body.classList.remove("is-data-loading");
  dataCard.setAttribute("aria-busy", "false");
  dataCard.classList.add("is-error");
  say(preloadState, "dataInterrupted");
  say(errorMessage, "errorHint", { message });
  errorPanel.hidden = false;
  say(loadStatus, "loadFailed");
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
  say(preloadState, "awaitingManifest");
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
  if (!activeVariable || !activeSession) return 0;
  const prefix = `${probeKey(activeSession, activeVariable)}:`;
  let cached = 0;
  for (const key of planeCache.keys()) {
    if (key.startsWith(prefix)) cached += 1;
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

/** How much of the grid the view is fetching, when it is fetching a subset —
 * empty on the whole-plane path, which is every non-streaming session and
 * every global view. */
function tileShare(): string {
  const geometry = activeSession?.tiles;
  const tiles = activeSession?.viewTiles;
  if (!geometry || !tiles) return "";
  return ` · ${tileCount(tiles)} / ${geometry.columns * geometry.rows} tiles`;
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
  const columns = grid ? `${needed} / ${grid.width} col` : `${needed} col`;
  statViewport.textContent = `${columns}${tileShare()}`;
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
    `lines: ${overlaySlots().map((slot) => `${slot.session!.id} (${slot.session!.format})`).join(", ") || "--"}`,
    `grid: ${session ? `${session.metadata.grid.width} × ${session.metadata.grid.height}` : "--"}`,
    `time: ${time ? `${time.frameCount}F · first ${time.firstForecastHour}h · ${time.stepHours !== undefined ? `step ${time.stepHours}h` : "mixed step"}` : "--"}`,
    `frame: ${activeFrameIndex === null ? "--" : formatLead(activeFrameIndex)} · ${playbackFps} fps`,
    `planes: ${cachedFrameCount()} / ${frameCount()} · ${formatBytes(planeCacheBytes)} / ${formatBytes(planeCacheBudgetBytes())}`,
    `network: ${session ? `${formatBytes(session.bytes)} / ${formatBytes(session.totalBytes)}${session.resident ? " · resident" : session.streaming ? " · streaming" : ""}` : "--"}`,
    `decode: ${lastDecodeMs === null ? "--" : `${lastDecodeMs.toFixed(1)} ms`} · ${formatBytes(decodeRateBytesPerSec())}/s`,
    `viewport: ${Math.round(neededGridWidth())} col${tileShare()} · zoom ${map.getZoom().toFixed(2)} · dpr ${window.devicePixelRatio || 1}`,
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
  // A passing notice: the status underneath keeps its message, so a language
  // switch during it restores the right one.
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

// The point probe: a click pins one grid cell and reads it across the whole
// time axis. On a container v2 bundle it asks for the series outright — one
// chunk per temporal group of the one tile holding the cell, a few dozen KB
// whatever the axis length — so the chart is complete the moment it opens.
// Where the container cannot address a cell (a v1 bundle, or the H.264 video
// path) it falls back to sampling every plane the app decodes for the screen
// anyway: the series fills in as playback or a scrub walks the axis, and an
// undecoded frame is just a gap. That keeps the probe honest under windowed
// streaming, where only the frames around the playhead are ever local.
let probe: ProbeSeries | null = null;
let probePopup: Popup | null = null;
let probeRenderFrame: number | null = null;
/** Series requests in flight, so a re-render or a session swap does not ask
 * twice for the same cell. Keyed by `variableId:column:row`. */
const probeSeriesRequests = new Set<string>();

const probePanel = buildProbePanel();

function buildProbePanel() {
  const root = document.createElement("div");
  root.className = "probe-panel";
  root.id = "probe-panel";
  root.setAttribute("aria-label", t("probeAria"));
  const head = document.createElement("div");
  head.className = "probe-head";
  const code = document.createElement("span");
  code.className = "probe-code";
  code.id = "probe-code";
  const coords = document.createElement("span");
  coords.className = "probe-coords";
  coords.id = "probe-coords";
  head.append(code, coords);
  const value = document.createElement("output");
  value.className = "probe-value";
  value.id = "probe-value";
  const meta = document.createElement("div");
  meta.className = "probe-meta";
  meta.id = "probe-meta";
  const canvas = document.createElement("canvas");
  canvas.className = "probe-chart";
  canvas.setAttribute("aria-hidden", "true");
  const footer = document.createElement("p");
  footer.className = "probe-footer";
  const count = document.createElement("span");
  count.className = "probe-count";
  count.id = "probe-count";
  const hint = document.createElement("span");
  hint.id = "probe-hint";
  footer.append(count, hint);
  root.append(head, value, meta, canvas, footer);
  return { root, code, coords, value, meta, canvas, count, hint };
}

/** A probed coordinate, at the precision a grid cell center needs. */
function formatProbeDegrees(value: number, axis: "NS" | "EW"): string {
  const hemisphere = axis === "NS" ? (value >= 0 ? "N" : "S") : (value >= 0 ? "E" : "W");
  return `${Math.abs(value).toFixed(2)}°${hemisphere}`;
}

/** Decimals worth showing for a value: a linear codebook resolves exactly as
 * far as its step, and the logarithmic one resolves light rain much more
 * finely than heavy. */
function formatProbeValue(variable: BundleVariable, value: number): string {
  if (variable.quantization.type === "linear") {
    const step = variable.quantization.scale;
    return value.toFixed(step >= 1 ? 0 : step >= 0.1 ? 1 : 2);
  }
  return value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2);
}

/** Pin a point and open its panel. Before a session exists there is no grid
 * to sample against, so a click is simply ignored. */
function setProbe(longitude: number, latitude: number): void {
  if (!activeSession) return;
  probe = new ProbeSeries(longitude, latitude);
  // The requests tracked for the previous pin say nothing about this cell,
  // and keeping them would suppress the series read when a point is pinned
  // again later.
  probeSeriesRequests.clear();
  seedProbeFromCache();
  requestProbeSeries();
  if (!probePopup) {
    probePopup = new Popup({
      closeButton: true,
      closeOnClick: false,
      closeOnMove: false,
      maxWidth: "none",
      className: "probe-popup",
      offset: 10,
    }).setDOMContent(probePanel.root);
    probePopup.on("close", () => {
      probe = null;
      probeSeriesRequests.clear();
    });
  }
  probePopup.setLngLat([longitude, latitude]);
  // Moving an open popup is setLngLat alone: addTo() on one that is already
  // on the map removes it first, and that fires `close` — dropping the pin
  // this call just made, so the panel would keep showing the old point.
  if (!probePopup.isOpen()) probePopup.addTo(map);
  renderProbe();
}

function closeProbe(): void {
  probe = null;
  probeSeriesRequests.clear();
  probePopup?.remove();
}

/** Ask the active session's decoder for the pinned cell's whole series, one
 * request per data variable. Only a tiled bundle can answer; everywhere else
 * the opportunistic sampling in `handleDecodedFrame` remains the only source.
 */
function requestProbeSeries(): void {
  const session = activeSession;
  if (!probe || !session || !session.tiles || !ready) return;
  const cell = probe.cellFor(session.metadata);
  if (!cell) return;
  for (const variable of session.variables) {
    const key = `${probeKey(session, variable)}:${cell.column}:${cell.row}`;
    if (probeSeriesRequests.has(key)) continue;
    probeSeriesRequests.add(key);
    session.worker.postMessage({
      type: "series",
      requestId: nextRequestId++,
      generation,
      variableId: variable.numericId,
      column: cell.column,
      row: cell.row,
    });
  }
}

/** A whole series arrived. It is dropped unless the pin is still on the very
 * cell it was read for — a new run, a new model, or a moved pin all make it
 * stale, and the codes are meaningless against another grid. */
function handleProbeSeries(
  session: VariableSession,
  message: {
    generation: number;
    variableId: number;
    column: number;
    row: number;
    buffer: ArrayBuffer;
  },
): void {
  const variable = session.variables.find((item) => item.numericId === message.variableId);
  if (!probe || !variable || message.generation !== generation) return;
  const cell = probe.cellFor(session.metadata);
  if (!cell || cell.column !== message.column || cell.row !== message.row) return;
  const offsets = frameOffsets(session.metadata.time);
  if (probe.adopt(session.metadata, probeKey(session, variable), offsets, new Uint8Array(message.buffer))) {
    scheduleProbeRender();
  }
}

/** Take the samples the frame cache already holds — a point pinned mid-run
 * starts with whatever playback has decoded so far, not an empty chart. */
function seedProbeFromCache(): void {
  if (!probe) return;
  for (const [key, frame] of planeCache) {
    const parts = parseFrameCacheKey(key);
    // The owning session travels with the plane; only the variable and the
    // frame have to be read back out of the key.
    const variable = parts && frame.session.variables.find((item) => item.numericId === parts.numericId);
    if (!parts || !variable) continue;
    probe.sample(frame.session.metadata, probeKey(frame.session, variable), parts.frameOffset, frame.plane);
  }
}

/** Coalesce the redraws that decode completions and frame steps both trigger
 * into one per animation frame. */
function scheduleProbeRender(): void {
  if (!probe || probeRenderFrame !== null) return;
  probeRenderFrame = window.requestAnimationFrame(() => {
    probeRenderFrame = null;
    renderProbe();
  });
}

function renderProbe(): void {
  const series = probe;
  if (!series) return;
  const session = activeSession;
  if (!session) {
    // Between datasets (a model switch, a new run) there is nothing to read:
    // blank the panel rather than leave the previous dataset's numbers up.
    probePanel.value.value = "--";
    probePanel.meta.textContent = t("probeAwaiting");
    probePanel.count.textContent = "";
    probePanel.hint.textContent = "";
    probePanel.canvas.getContext("2d")?.clearRect(0, 0, probePanel.canvas.width, probePanel.canvas.height);
    return;
  }
  const variable = session.variable;
  const cell = series.cellFor(session.metadata);
  probePanel.code.textContent = variableUi(session).code;
  const point = cell ?? { longitude: series.longitude, latitude: series.latitude };
  probePanel.coords.textContent =
    `${formatProbeDegrees(point.latitude, "NS")} ${formatProbeDegrees(point.longitude, "EW")}`;

  const index = activeFrameIndex ?? Number(slider.value);
  const offsets = frameAxis();
  const probed = probeVariables(session);
  const values = cell ? probeSeriesValues(series, probed, offsets) : [];
  const current = values[index];

  if (!cell) {
    probePanel.value.value = "--";
    probePanel.meta.textContent = t("probeOutside");
  } else {
    probePanel.value.value =
      typeof current === "number" ? `${formatProbeValue(variable, current)} ${variable.unit}` : "--";
    const lead = `${formatLead(index)} · ${formatCompactDate(frameValidTime(index))}`;
    if (current === undefined) probePanel.meta.textContent = `${lead} · ${t("probeAwaiting")}`;
    else if (current === null) probePanel.meta.textContent = `${lead} · ${t("probeNoData")}`;
    else {
      // Wind's series is the speed; the direction only means anything for the
      // frame on screen, so it rides the lead-time line.
      const direction = session.vector ? probeWindDirection(series, probed, frameOffset(index)) : null;
      probePanel.meta.textContent = direction === null
        ? lead
        : `${lead} · ${String(Math.round(direction)).padStart(3, "0")}°`;
    }
  }

  const sampled = values.reduce<number>((total, value) => total + (value === undefined ? 0 : 1), 0);
  probePanel.count.textContent = cell ? `${sampled} / ${offsets.length}` : "";
  probePanel.hint.textContent = !cell ? "" : sampled >= offsets.length ? t("probeComplete") : t("probeHint");
  drawProbeChart(values, index, variable);
}

/** The series as a sparkline: sampled frames joined, gaps left open, the
 * playhead marked. Values are quantized, so the ladder in a flat stretch is
 * the codebook's own step, not noise. */
function drawProbeChart(values: ProbeValue[], selected: number, variable: BundleVariable): void {
  const canvas = probePanel.canvas;
  const context = canvas.getContext("2d");
  if (!context) return;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (width === 0 || height === 0) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);

  const styles = getComputedStyle(document.body);
  const accent = styles.getPropertyValue("--accent").trim() || "#54d6c7";
  const mutedColor = styles.getPropertyValue("--muted").trim() || "#8ca6b2";
  context.font = "8px 'IBM Plex Mono', monospace";
  context.textBaseline = "middle";

  // A left gutter carries the value range, so the plot never runs under it.
  const gutter = 30;
  const top = 6;
  const bottom = height - 6;
  const plotWidth = Math.max(1, width - gutter);
  let lowest = Infinity;
  let highest = -Infinity;
  for (const value of values) {
    if (typeof value !== "number") continue;
    lowest = Math.min(lowest, value);
    highest = Math.max(highest, value);
  }
  if (lowest === Infinity) {
    context.fillStyle = mutedColor;
    context.textAlign = "center";
    context.fillText("--", width / 2, height / 2);
    return;
  }
  // A series that never moves (dry precipitation, a still night) still gets a
  // line, centered rather than divided by a zero range.
  const flat = highest - lowest < 1e-9;
  const span = flat ? 1 : highest - lowest;
  const base = flat ? lowest - 0.5 : lowest;

  const x = (index: number): number =>
    gutter + (values.length > 1 ? (index / (values.length - 1)) * plotWidth : plotWidth / 2);
  const y = (value: number): number => bottom - ((value - base) / span) * (bottom - top);

  context.strokeStyle = mutedColor;
  context.globalAlpha = 0.22;
  context.lineWidth = 1;
  for (const edge of [top, bottom]) {
    context.beginPath();
    context.moveTo(gutter, edge + 0.5);
    context.lineTo(width, edge + 0.5);
    context.stroke();
  }

  // The playhead sits under the series so the line stays readable across it.
  context.globalAlpha = 0.5;
  context.beginPath();
  context.moveTo(Math.round(x(selected)) + 0.5, top);
  context.lineTo(Math.round(x(selected)) + 0.5, bottom);
  context.stroke();
  context.globalAlpha = 1;

  context.strokeStyle = accent;
  context.lineWidth = 1.5;
  context.lineJoin = "round";
  context.beginPath();
  let drawing = false;
  for (const [index, value] of values.entries()) {
    if (typeof value !== "number") {
      drawing = false;
      continue;
    }
    if (drawing) context.lineTo(x(index), y(value));
    else context.moveTo(x(index), y(value));
    drawing = true;
  }
  context.stroke();

  const current = values[selected];
  if (typeof current === "number") {
    context.fillStyle = accent;
    context.beginPath();
    context.arc(x(selected), y(current), 2.5, 0, 2 * Math.PI);
    context.fill();
  }

  context.fillStyle = mutedColor;
  context.textAlign = "right";
  context.fillText(formatProbeValue(variable, highest), gutter - 5, top);
  if (!flat) context.fillText(formatProbeValue(variable, lowest), gutter - 5, bottom);
}

/** Sync the data card with one session's delivery state. The card only reads
 * complete for a whole bundle: a narrowed session has every byte its view
 * needs but nowhere near every byte of the file, and the progress bar behind
 * the label counts real bytes. */
function refreshDataCard(session: VariableSession): void {
  updateDownloadProgress(session.bytes, session.totalBytes);
  document.body.classList.remove("is-data-loading");
  dataCard.setAttribute("aria-busy", "false");
  const whole = session.resident && session.residentScope === "bundle";
  dataCard.classList.toggle("is-complete", whole);
  say(preloadState, session.resident ? (whole ? "bundleResident" : "viewportResident") : "streamingOnDemand");
}

/** Handles `progress`/`resident` messages from streaming decode channels. */
function handleStreamMessage(message: {
  type: string;
  variableKey?: unknown;
  bytes?: unknown;
  totalBytes?: unknown;
  scope?: unknown;
}): void {
  const id = message.variableKey;
  if (!isBundleVariableId(id)) return;
  const bundleId = id;
  const session = sessions.get(bundleId);
  if (!session) {
    const entry = pendingStream.get(bundleId) ?? { bytes: 0, resident: false, scope: "bundle" as const };
    if (message.type === "progress" && typeof message.bytes === "number") entry.bytes = message.bytes;
    if (message.type === "resident") {
      entry.resident = true;
      entry.scope = message.scope === "viewport" ? "viewport" : "bundle";
    }
    pendingStream.set(bundleId, entry);
    return;
  }
  if (message.type === "progress" && typeof message.bytes === "number") {
    session.bytes = Math.min(session.totalBytes, session.extraBytes + message.bytes);
  }
  if (message.type === "resident") {
    session.resident = true;
    session.residentScope = message.scope === "viewport" ? "viewport" : "bundle";
    // With the whole bundle local, narrowing to the view buys nothing any more.
    if (session.residentScope === "bundle" && slotSessions().includes(session)) refreshViewportTiles();
  }
  if (activeSession?.id === bundleId) refreshDataCard(session);
}

function updateTransport(): void {
  playButton.classList.toggle("is-playing", playing);
  const transportLabel = playing ? t("pauseAnimation") : t("playAnimation");
  playButton.setAttribute("aria-label", transportLabel);
  playButton.title = transportLabel;
  playLabel.textContent = playing ? "PAUSE" : "PLAY";
}

/** (Re)start the frame cadence on the frame now on screen, holding it for its
 * own forecast step. */
function restartCadence(index: number): void {
  currentHoldMs = frameHoldMs(index);
  nextFrameAt = performance.now() + currentHoldMs;
}

/** Adopt a playback rate: relabel the button and re-base the cadence so the
 * change lands on the next frame rather than after the interval the old rate
 * already scheduled. `remember` marks it as the viewer's own choice. */
function setPlaybackFps(fps: PlaybackFps, remember: boolean): void {
  playbackFps = fps;
  frameIntervalMs = 1000 / fps;
  speedLabel.textContent = `${fps} FPS`;
  if (playing) restartCadence(activeFrameIndex ?? Number(slider.value));
  if (!remember) return;
  playbackFpsChosen = true;
  try {
    localStorage.setItem(PLAYBACK_FPS_KEY, String(fps));
  } catch {
    // Preference just won't persist.
  }
}

function stopPlayback(): void {
  playing = false;
  if (playbackFrame !== null) {
    window.cancelAnimationFrame(playbackFrame);
    playbackFrame = null;
  }
  updateTransport();
  // Playback throttles the labels; the frame it stopped on gets them now.
  refreshLabels();
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
      // Under one hold late is normal rAF quantization: keep the fixed
      // cadence so playback averages exactly the selected rate. Later than
      // that was a decode/network stall — re-base so the following frame
      // waits its whole hold instead of a leftover fraction of one.
      const elapsedHold = currentHoldMs;
      currentHoldMs = frameHoldMs(next);
      nextFrameAt = timestamp - nextFrameAt < elapsedHold
        ? nextFrameAt + currentHoldMs
        : timestamp + currentHoldMs;
    }
    // When the frame is still decoding, hold the current image; the decode
    // completion will pull playback forward without touching opacity.
  } else {
    blendTowardNext(timestamp);
  }
  playbackFrame = window.requestAnimationFrame(advancePlayback);
}

/** Between frame steps, sweep the shader blend weight toward the
 * next frame's plane so playback reads as continuous motion. Skipped across
 * the loop seam (last -> first frame is a restart, not a transition) and
 * whenever either plane is not decoded yet — the current image just holds.
 *
 * Wind blends like every other field: its codes mix linearly, and taking the
 * magnitude after the mix is a blend of the two wind vectors rather than of
 * two speeds. (It used to be skipped, back when the particles were the whole
 * visual and supplied the motion between field updates themselves.) */
function blendTowardNext(timestamp: number): void {
  const session = activeSession;
  const slot = primarySlot();
  if (!session || !slot || !activeVariable || activeFrameIndex === null) return;
  if (slot.displayedReal !== session.id) return;
  const next = activeFrameIndex + 1;
  if (next >= frameCount()) return;
  const weight = 1 - (nextFrameAt - timestamp) / currentHoldMs;
  const current = framePlanes(session, activeFrameIndex);
  const upcoming = framePlanes(session, next);
  // One coverage box serves both slots, so a pair decoded for different views
  // (a pan mid-playback) holds the current image instead of blending a plane
  // against another frame's stale bytes.
  if (current && upcoming && sameTileRects(current[0]!.tiles, upcoming[0]!.tiles)) {
    ensureSlotGrid(slot, session);
    slot.layer.setBlend(
      displayPlane(session, activeFrameIndex, current),
      displayPlane(session, next, upcoming),
      weight,
      sessionCoverage(session, current[0]!.tiles),
    );
  }
  // The lines blend their own pair on the same clock, so at any instant the
  // two slots sit at the same fraction of the step. An overlay still catching
  // up (its plane for this frame not decoded, or not on its axis) holds.
  for (const overlay of overlaySlots()) blendOverlay(overlay, activeFrameIndex, next, weight);
}

/** Sweep one overlay between the planes it holds for two primary frames,
 * found by lead time on its own axis. Nothing happens unless the overlay is
 * showing exactly the current frame and has the next one cached too. */
function blendOverlay(slot: RasterSlot, index: number, next: number, weight: number): void {
  const session = slot.session;
  if (!session || slot.displayedReal !== session.id) return;
  const offsetA = sessionOffsetForLead(session, frameLeadSeconds(index));
  const offsetB = sessionOffsetForLead(session, frameLeadSeconds(next));
  if (offsetA === null || offsetB === null) return;
  const keyA = cacheKey(session, session.variable, offsetA);
  if (slot.shownKey !== keyA) return;
  const planeA = planeCache.get(keyA);
  const planeB = planeCache.get(cacheKey(session, session.variable, offsetB));
  if (!planeA || !planeB || !sameTileRects(planeA.tiles, planeB.tiles)) return;
  ensureSlotGrid(slot, session);
  slot.layer.setBlend(planeA.plane, planeB.plane, weight, sessionCoverage(session, planeA.tiles));
}

function startPlayback(): void {
  if (!activeVariable || slider.disabled || playing || !ready) return;
  hideError();
  playing = true;
  restartCadence(activeFrameIndex ?? Number(slider.value));
  updateTransport();
  playbackFrame = window.requestAnimationFrame(advancePlayback);
}

function updateFrameReadout(index: number): void {
  const lead = formatLead(index);
  const valid = frameValidTime(index);
  slider.value = String(index);
  slider.setAttribute("aria-valuetext", `${lead}, ${formatDate(valid)}`);
  forecastLead.value = lead;
  validTime.textContent = formatDate(valid);
  frameTooltip.value = `${lead} · ${formatCompactDate(valid)}`;
  // Read by the tooltip and by the track's playhead, so it is set on the
  // capsule both of them sit in.
  timelinePanel.style.setProperty("--frame-progress", `${(index / Math.max(1, frameCount() - 1)) * 100}%`);
  updateTicks(index);
  updateForecastDay(index);
  scheduleProbeRender();
}

/** Reconfigure a slot's layer for its session's own bundle grid (poster
 * grid, full grid, and variant grids all differ) before showing a real
 * plane of that session. */
function ensureSlotGrid(slot: RasterSlot, session: VariableSession): void {
  if (slot.gridSource === session.metadata) return;
  slot.layer.configureGrid(session.metadata);
  slot.gridSource = session.metadata;
}

/** Same for the particle layer: adopt the vector session's grid, its u/v
 * quantization and its magnitude ceiling before feeding it planes. */
function ensureWindGrid(session: VariableSession): void {
  if (!windLayer || windLayerGridSource === session.metadata) return;
  // The pair comes off the session's own variables — the file's u and v, in
  // the order its parameter blocks put them.
  const [u, v] = session.variables;
  windLayer.configureGrid(session.metadata, u && v ? [u.id, v.id] : undefined);
  windLayer.setMaxSpeed(sessionMaxMagnitude(session));
  windLayerGridSource = session.metadata;
}

// The tiles a session decodes for the current view, or null for the whole
// plane. Narrowing only pays where bytes are still being fetched, so it is
// limited to a streaming session that is not resident yet; a downloaded
// bundle has already paid for every byte. Wind narrows like anything else
// while its particle overlay is off: the filled speed field only needs what
// the view shows, but the particles respawn anywhere on the grid, so with
// them on the session takes the whole plane. Each session on screen keeps
// its own answer in `viewTiles`.
function sessionViewportTiles(session: VariableSession): TileRect[] | null {
  if (!session.tiles || !session.streaming) return null;
  if (session.resident && session.residentScope === "bundle") return null;
  if (session.vector && particlesEnabled) return null;
  const bounds = map.getBounds();
  return viewportTileRects(session.metadata, session.tiles, {
    west: bounds.getWest(),
    east: bounds.getEast(),
    south: bounds.getSouth(),
    north: bounds.getNorth(),
  });
}

/** Recompute the view's tiles after a pan, a zoom, or a session change, for
 * every session on screen. A plane already decoded for a wider set stays
 * valid; anything narrower is re-requested by the frame retry below. */
function refreshViewportTiles(): void {
  let changed = false;
  for (const session of slotSessions()) {
    const next = sessionViewportTiles(session);
    if (sameTileRects(next, session.viewTiles)) continue;
    session.viewTiles = next;
    changed = true;
    // The window is keyed by its first hour alone, so force the next send.
    lastPrefetchWindow.delete(session.id);
    if (session.resident && session.residentScope === "viewport") {
      // The view moved, so what it needs is no longer all local.
      session.resident = false;
      if (session === activeSession) refreshDataCard(session);
    }
  }
  if (!changed) return;
  updateStatsReadout();
  const target = requestedFrameIndex ?? activeFrameIndex;
  if (target !== null) trySelectFrame(target);
}

/** The texture box a plane's tiles fill on its own session grid — what the
 * shader clips to, so the stale bytes outside them are never painted. */
function sessionCoverage(session: VariableSession, tiles: TileRect[] | null): CoverageBox {
  if (!tiles || !session.tiles) return WHOLE_PLANE_COVERAGE;
  const grid = session.metadata.grid;
  return coverageBox(session.tiles, grid.width, grid.height, tiles);
}

/** The plane cached under `key`, but only if it covers the session's current
 * view. A plane decoded for a viewport that has since moved is not a hit. */
function cachedFrame(key: string, session: VariableSession): DecodedFrame | undefined {
  const frame = planeCache.get(key);
  return frame && coversTiles(frame.tiles, session.viewTiles) ? frame : undefined;
}

/** Every plane one frame of this session needs, straight out of the cache —
 * the u/v pair for wind, one plane otherwise — or null when any is missing.
 * A pair decoded for different tile sets is refused too: the two channels
 * share one coverage box, and the wider of them would be read from stale
 * bytes outside the narrower one. */
function framePlanes(session: VariableSession, index: number): DecodedFrame[] | null {
  const hour = frameOffset(index);
  const planes: DecodedFrame[] = [];
  for (const variable of session.variables) {
    const frame = planeCache.get(cacheKey(session, variable, hour));
    if (!frame) return null;
    if (planes.length > 0 && !sameTileRects(planes[0]!.tiles, frame.tiles)) return null;
    planes.push(frame);
  }
  return planes;
}

/** Interleaved wind planes, keyed by the frame they belong to. Packing a
 * 1440x721 pair copies two megabytes, and a blend sweep asks for the same two
 * frames on every animation frame, so the result is kept and reused as long
 * as it was built from the very planes still in the cache. Four is the blend's
 * two plus the step moving onto the next pair. */
const vectorPlanes = new Map<string, { sources: Uint8Array[]; packed: Uint8Array }>();
const VECTOR_PLANE_CACHE = 4;

/** The bytes the scalar layer draws for one frame: the plane itself for a
 * scalar session, and for wind the u/v pair interleaved into one RG plane —
 * u codes in red, v in green, the same packing the particle layer builds for
 * its own texture, which is what the layer's magnitude mode reads. */
function displayPlane(session: VariableSession, index: number, planes: DecodedFrame[]): Uint8Array {
  if (!session.vector || planes.length < 2) return planes[0]!.plane;
  const key = cacheKey(session, session.variables[0]!, frameOffset(index));
  const sources = planes.map((frame) => frame.plane);
  const held = vectorPlanes.get(key);
  if (held && held.sources.length === sources.length && held.sources.every((plane, at) => plane === sources[at])) {
    return held.packed;
  }
  const [u, v] = sources as [Uint8Array, Uint8Array];
  const packed = new Uint8Array(u.length * 2);
  for (let cell = 0; cell < u.length; cell += 1) {
    packed[cell * 2] = u[cell]!;
    packed[cell * 2 + 1] = v[cell]!;
  }
  vectorPlanes.set(key, { sources, packed });
  // Oldest first, and never the one just built.
  while (vectorPlanes.size > VECTOR_PLANE_CACHE) {
    const oldest = vectorPlanes.keys().next().value;
    if (oldest === undefined || oldest === key) break;
    vectorPlanes.delete(oldest);
  }
  return packed;
}

/** Tell every session on screen which frames to keep resident (windowed
 * prefetch): the window ahead of the playhead, wrapped at the loop point,
 * and the tiles the view needs of them. The primary gets the connection's
 * concurrency; an overlay one fetch at a time, so it never crowds out the
 * frame that gates playback, and on a constrained connection an overlay
 * only prefetches while playback is stopped. */
function sendPrefetchWindow(index: number): void {
  const total = frameCount();
  for (const session of slotSessions()) {
    if (!session.streaming || session.resident) continue;
    const overlay = session !== activeSession;
    if (overlay && playing && constrainedConnection()) continue;
    const hours: number[] = [];
    for (let step = 0; step <= prefetchWindowFrames(); step += 1) {
      const frame = (index + step) % total;
      const offset = overlay ? sessionOffsetForLead(session, frameLeadSeconds(frame)) : frameOffset(frame);
      if (offset !== null) hours.push(offset);
    }
    if (hours.length === 0) continue;
    const key = String(hours[0]);
    if (lastPrefetchWindow.get(session.id) === key) continue;
    lastPrefetchWindow.set(session.id, key);
    session.worker.postMessage({
      type: "prefetch-window",
      hours,
      concurrency: overlay ? 1 : prefetchConcurrency(),
      tiles: session.viewTiles ?? undefined,
    });
  }
}

/** Show the frame if every needed plane is decoded (one for scalars, the u/v
 * pair for wind); otherwise request the missing decodes. */
function trySelectFrame(index: number): boolean {
  const session = activeSession;
  const slot = primarySlot();
  if (!session || !slot || !layersAdded) return false;
  const hour = frameOffset(index);
  requestedFrameIndex = index;
  updateFrameReadout(index);
  sendPrefetchWindow(index);
  const keys = session.variables.map((variable) => cacheKey(session, variable, hour));
  const planes = keys.map((key) => cachedFrame(key, session));
  if (planes.every((plane) => plane !== undefined)) {
    // Refresh LRU positions, then swap textures — no opacity involved.
    for (const [position, key] of keys.entries()) {
      planeCache.delete(key);
      planeCache.set(key, planes[position]!);
    }
    ensureSlotGrid(slot, session);
    if (session.vector) {
      // One coverage box serves both channels, so it has to be a box both
      // planes hold: their own tiles when the pair agrees, and the view's
      // otherwise — cachedFrame has already proved every plane covers that.
      const tiles = sameTileRects(planes[0]!.tiles, planes[1]!.tiles) ? planes[0]!.tiles : session.viewTiles;
      slot.layer.setFrame(displayPlane(session, index, planes as DecodedFrame[]), sessionCoverage(session, tiles));
      // The particles respawn anywhere on the grid, so they can only run on
      // whole planes. A narrowed session draws the field alone until the
      // overlay is switched back on and widens the session again.
      if (windLayer && planes.every((plane) => plane!.tiles === null)) {
        ensureWindGrid(session);
        windLayer.setWindPlanes(planes[0]!.plane, planes[1]!.plane);
      }
    } else {
      slot.layer.setFrame(planes[0]!.plane, sessionCoverage(session, planes[0]!.tiles));
      if (slot === slots.lines) scheduleLabels(planes[0]!, false);
    }
    slot.displayedReal = session.id;
    slot.shownKey = keys[0]!;
    activeFrameIndex = index;
    desiredKey = null;
    // The overlays follow the frame that is actually on screen, not the one
    // asked for: a line chart of one hour over a field of another would be
    // the wrong picture, whichever of the two were ahead.
    for (const overlay of overlaySlots()) trySelectOverlayFrame(overlay, index);
    prefetchNext(index);
    return true;
  }
  // Track the first missing plane; when it arrives the frame is retried,
  // which walks desiredKey to the next still-missing plane (if any).
  desiredKey = keys.find((_, position) => planes[position] === undefined) ?? null;
  for (const [position] of keys.entries()) {
    if (planes[position] !== undefined) continue;
    requestDecode(session, session.variables[position]!, hour);
  }
  // The overlays' planes for this frame are asked for now too, after the
  // primary's, so they are as likely to be there when it lands.
  for (const overlay of overlaySlots()) requestOverlayDecode(overlay, index);
  return false;
}

/** The overlay's frame offset for a primary frame, or null when its axis has
 * no frame at that lead time. */
function overlayOffset(slot: RasterSlot, index: number): number | null {
  return slot.session ? sessionOffsetForLead(slot.session, frameLeadSeconds(index)) : null;
}

/** Show an overlay's plane for the primary frame at `index` if it is
 * decoded; otherwise keep what it has and ask for the plane. A frame that is
 * not on the overlay's axis at all hides the slot: "not decoded yet" and
 * "does not exist" are different — the first is a slightly old chart, the
 * second would be a chart of another hour drawn over this one. */
function trySelectOverlayFrame(slot: RasterSlot, index: number): void {
  const session = slot.session;
  if (!session) return;
  const offset = overlayOffset(slot, index);
  if (offset === null) {
    slot.layer.setVisible(false);
    slot.wantedKey = null;
    if (slot === slots.lines) clearLabels();
    return;
  }
  const key = cacheKey(session, session.variable, offset);
  const frame = cachedFrame(key, session);
  if (!frame) {
    slot.wantedKey = key;
    requestDecode(session, session.variable, offset);
    return;
  }
  planeCache.delete(key);
  planeCache.set(key, frame);
  ensureSlotGrid(slot, session);
  slot.layer.setFrame(frame.plane, sessionCoverage(session, frame.tiles));
  slot.layer.setVisible(true);
  slot.displayedReal = session.id;
  slot.shownKey = key;
  slot.wantedKey = null;
  if (slot === slots.lines) scheduleLabels(frame, false);
}

/** Ask for an overlay's plane for a primary frame without showing anything. */
function requestOverlayDecode(slot: RasterSlot, index: number): void {
  const session = slot.session;
  const offset = overlayOffset(slot, index);
  if (!session || offset === null) return;
  const key = cacheKey(session, session.variable, offset);
  if (!cachedFrame(key, session)) requestDecode(session, session.variable, offset);
}

/** Frames decoded ahead of the playhead during playback. One is not enough:
 * a single slow decode (byte-budget eviction forces perpetual re-decodes on
 * long loops) then stalls the very next frame step. A small pipeline absorbs
 * that jitter; the inflight cap in requestDecode still bounds worker load. */
const PLAYBACK_DECODE_AHEAD = 3;

function prefetchNext(index: number): void {
  const session = activeSession;
  if (!session || !playing) return;
  for (let step = 1; step <= PLAYBACK_DECODE_AHEAD; step += 1) {
    const frame = (index + step) % frameCount();
    const hour = frameOffset(frame);
    for (const variable of session.variables) {
      const key = cacheKey(session, variable, hour);
      if (!cachedFrame(key, session)) requestDecode(session, variable, hour);
    }
    for (const overlay of overlaySlots()) requestOverlayDecode(overlay, frame);
  }
}

/** Ask one session's decoder for one of its variables at one frame offset.
 * The session is passed in rather than looked up: a numericId names a
 * variable only inside the file it came from. */
function requestDecode(session: VariableSession, variable: BundleVariable, hour: number): void {
  if (!ready) return;
  const key = cacheKey(session, variable, hour);
  if ([...inflight.values()].includes(key)) return;
  // The wind session needs two planes per frame and an overlay adds its own,
  // so the inflight cap scales with the planes one frame of the view needs.
  if (inflight.size >= 2 * planesPerFrame()) {
    // Keep only the newest queued request while scrubbing.
    queuedRequest = { session, variable, hour };
    return;
  }
  const requestId = nextRequestId++;
  inflight.set(requestId, key);
  session.worker.postMessage({
    type: "decode",
    requestId,
    generation,
    variableId: variable.numericId,
    frameOffset: hour,
    // Only a session on screen narrows the decode to its view; a background
    // session (the other variable, preloading) is asked for whole planes.
    tiles: (slotSessions().includes(session) ? session.viewTiles : null) ?? undefined,
  });
}

/** Planes one frame of the whole view needs: the u/v pair for wind, one per
 * scalar, summed over the slots on screen. */
function planesPerFrame(): number {
  let planes = 0;
  for (const session of slotSessions()) planes += session.variables.length;
  return Math.max(1, planes);
}

function handleDecodedFrame(
  session: VariableSession,
  message: {
    /** Absent on cache-warm-up frames the video path decodes alongside a target. */
    requestId?: number;
    variableId: number;
    frameOffset: number;
    decodeMs: number;
    /** Absent when the whole plane is valid — the video path never narrows. */
    tiles?: TileRect[];
    buffer: ArrayBuffer;
  },
): void {
  if (typeof message.requestId === "number") inflight.delete(message.requestId);
  // The message names a variable of *its own* file; the session it arrived
  // on is what makes that a key.
  const variable = session.variables.find((item) => item.numericId === message.variableId);
  if (!variable) return;
  const key = cacheKey(session, variable, message.frameOffset);
  const previous = planeCache.get(key);
  if (previous) planeCacheBytes -= previous.plane.byteLength;
  const plane = new Uint8Array(message.buffer);
  planeCache.set(key, { plane, decodeMs: message.decodeMs, tiles: message.tiles ?? null, session });
  planeCacheBytes += plane.byteLength;
  lastDecodeMs = message.decodeMs;
  recordDecodeEvent(plane.byteLength, message.decodeMs);
  // The probe reads its cell here, before the eviction below can recycle this
  // plane back into the worker: one byte is kept, never the plane.
  if (probe && probe.sample(session.metadata, probeKey(session, variable), message.frameOffset, plane)) {
    scheduleProbeRender();
  }
  // Evict by byte budget, oldest first; never evict the just-inserted
  // frame or the ones on screen (the blend path may still sample them; wind
  // keeps a u/v pair displayed).
  const displayedKeys = new Set<string>();
  if (activeSession && activeFrameIndex !== null) {
    for (const variable of activeSession.variables) {
      displayedKeys.add(cacheKey(activeSession, variable, frameOffset(activeFrameIndex)));
    }
  }
  for (const overlay of overlaySlots()) {
    if (overlay.shownKey !== null) displayedKeys.add(overlay.shownKey);
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
    // The buffer goes back to the worker that produced it — which the frame
    // itself names, not the key it was filed under.
    const buffer = evicted.plane.buffer as ArrayBuffer;
    evicted.session.worker.postMessage({ type: "recycle", buffer }, [buffer]);
  }
  updateCacheReadout();

  if (queuedRequest) {
    const queued = queuedRequest;
    queuedRequest = null;
    const queuedKey = cacheKey(queued.session, queued.variable, queued.hour);
    if (queuedKey !== key && !planeCache.has(queuedKey)) {
      requestDecode(queued.session, queued.variable, queued.hour);
    }
  }
  // Display only the newest requested target; stale decodes stay cached.
  if (desiredKey === key && activeVariable) {
    const index = frameIndexForOffset(message.frameOffset);
    if (index < 0) return;
    const shown = trySelectFrame(index);
    // A stalled playhead resumes here, off the decode completion, so restart
    // the cadence too: without this the next rAF tick sees a long-expired
    // deadline and steps again immediately — the fast half of the visible
    // fast/slow playback jitter.
    if (shown && playing) restartCadence(index);
    return;
  }
  // An overlay's plane for the frame on screen: swap it in now.
  for (const overlay of overlaySlots()) {
    if (overlay.wantedKey === key && activeFrameIndex !== null) trySelectOverlayFrame(overlay, activeFrameIndex);
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
    tick.className = frameLeadSeconds(index) % DAY_SECONDS === 0 ? "major" : "";
    tickMarks.append(tick);
  }
}

/** Frame index of one forecast day boundary (24, 48, ... hours out), or null
 * when the model's step does not land a frame exactly on it. */
function dayFrameIndex(day: number): number | null {
  if (!metadata) return day * 24 < FRAME_COUNT ? day * 24 : null;
  const index = frameIndexForOffset((day * DAY_SECONDS) / frameUnitSeconds);
  return index >= 0 ? index : null;
}

/** Whole forecast days the active axis reaches (5 on a 120-hour run, 10 on
 * a 240-hour one). */
function forecastDayCount(): number {
  return Math.floor(frameLeadSeconds(frameCount() - 1) / DAY_SECONDS);
}

/** Day boundaries as marks along the track, each sitting at the fraction of
 * the axis its frame falls on. Ten of them on a 240-hour run would collide,
 * so a long axis labels every other day; a mark that would hang off either
 * end is dropped, since `.track-ends` already names both. */
function buildForecastDays(): void {
  forecastDays.replaceChildren();
  const days = forecastDayCount();
  const stride = days > 6 ? 2 : 1;
  const lastIndex = Math.max(1, frameCount() - 1);
  for (let day = stride; day <= days; day += stride) {
    const index = dayFrameIndex(day);
    if (index === null) continue;
    const percent = (index / lastIndex) * 100;
    if (percent < 4 || percent > 96) continue;
    const mark = document.createElement("time");
    mark.className = "forecast-day";
    mark.dataset.day = String(day);
    mark.style.left = `${percent.toFixed(2)}%`;
    const valid = frameValidTime(index);
    mark.dateTime = new Date(valid).toISOString();
    mark.textContent = formatDayMark(valid);
    forecastDays.append(mark);
  }
  updateForecastDay(0);
}

function updateForecastDay(frameIndex: number): void {
  const day = Math.ceil(frameLeadSeconds(frameIndex) / DAY_SECONDS);
  // The playhead belongs to the first mark it has not passed yet — the day
  // it is running into — and to the last mark once it is past all of them.
  const marks = [...forecastDays.children] as HTMLElement[];
  const active = marks.find((mark) => Number(mark.dataset.day) >= day) ?? marks.at(-1) ?? null;
  for (const mark of marks) {
    const on = mark === active;
    mark.classList.toggle("is-active", on);
    if (on) mark.setAttribute("aria-current", "step");
    else mark.removeAttribute("aria-current");
  }
}

/** Whether the layer switches are locked, so a tile built while a run is
 * still loading opens in the same state as the ones already on the rail. */
let variableButtonsDisabled = false;

function setVariableButtonsDisabled(disabled: boolean): void {
  variableButtonsDisabled = disabled;
  for (const button of variableButtons()) button.disabled = disabled;
  for (const button of modelButtons) button.disabled = disabled;
  for (const button of levelRow.querySelectorAll("button")) button.disabled = disabled;
}

/** One group of the level row: the members of a family the run publishes,
 * for one slot. */
interface LevelGroup {
  caption: string;
  slot: RasterSlot["role"];
  members: ForecastBundleId[];
  /** The pressed member; null for the lines group over a field with no
   * lines on it. */
  active: ForecastBundleId | null;
}

/** The level row's groups for the composition on screen: the fill's family
 * when it has more than one published member, then the lines. Over a field
 * the lines group is on the row whenever the run has a surface to chart,
 * with no member pressed while the lines are off — so the overlay is one
 * press away, and one press back after it was taken off. As the view itself
 * the lines are the one group, under the generic caption. */
function levelGroups(): LevelGroup[] {
  if (!manifest) return [];
  const run = manifest;
  const groups: LevelGroup[] = [];
  const fill = composition.fill;
  const fillFamily = fill === null ? null : familyOf(fill);
  if (fill !== null && fillFamily !== null) {
    const members = familyMembers(fillFamily).filter((id) => hasBundle(run, id));
    if (members.length > 1) groups.push({ caption: FAMILIES[fillFamily].code, slot: "fill", members, active: fill });
  }
  const surfaces = PRESSURE_BUNDLE_IDS.filter((id) => hasBundle(run, id));
  if (fill !== null) {
    if (surfaces.length > 0) groups.push({ caption: t("linesCaption"), slot: "lines", members: surfaces, active: composition.lines });
  } else if (composition.lines !== null) {
    groups.push({ caption: t("levelCaption"), slot: "lines", members: surfaces, active: composition.lines });
  }
  if (groups.length === 1 && groups[0]!.slot === "fill") groups[0]!.caption = t("levelCaption");
  return groups;
}

/** The visually hidden name of one level button: the instrument code and its
 * gloss, the same shape the rail tiles carry. */
function levelButtonName(id: ForecastBundleId): [string, string] {
  if (isPressureBundle(id)) {
    return id === "prmsl" ? ["MSLP", t("varPressure")] : [`${bundleLevel(id)}MB`, t("varHeight")];
  }
  const family = familyOf(id);
  if (family !== null && bundleLevel(id) === null) {
    const { code, glossKey, members } = FAMILIES[family];
    // A listed member (the cloud layers) is named for itself; a surface
    // member repeats its family tile's gloss.
    const gloss = members || glossKey === null ? familyLabel(id) : t(glossKey);
    return [`${code} ${levelCode(id)}`, gloss];
  }
  return [isobaricCode(id), familyLabel(id)];
}

/** Rebuild the level row for the composition on screen. The buttons are
 * generated from the manifest rather than written out: every family has
 * eight surfaces, and a run publishes a few of each. */
function renderLevelRow(): void {
  const groups = levelGroups();
  levelRow.hidden = groups.length === 0;
  levelRow.replaceChildren();
  for (const group of groups) {
    const container = document.createElement("div");
    container.className = "level-group";
    container.dataset.slot = group.slot;
    const caption = document.createElement("span");
    caption.className = "level-caption";
    caption.textContent = group.caption;
    container.append(caption);
    for (const id of group.members) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.variable = id;
      button.dataset.level = "";
      button.dataset.slot = group.slot;
      button.disabled = switchingVariable;
      button.setAttribute("aria-pressed", String(id === group.active));
      const glyph = document.createElement("b");
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = levelCode(id);
      const name = document.createElement("span");
      name.className = "rail-name";
      const [code, gloss] = levelButtonName(id);
      const codeSpan = document.createElement("span");
      codeSpan.textContent = code;
      const glossSmall = document.createElement("small");
      glossSmall.textContent = gloss;
      name.append(codeSpan, " ", glossSmall);
      button.append(glyph, name);
      // Over a filled field the lines are an overlay, and the pressed member
      // is also its off switch: pressing it again takes the lines away. As
      // the view itself the lines cannot be switched off, only changed.
      const removable = group.slot === "lines" && id === group.active && composition.fill !== null;
      if (removable) {
        button.classList.add("is-removable");
        button.title = t("linesRemoveTitle");
      }
      button.addEventListener("click", () => {
        // A level changes its own slot: the fill's family member, or the
        // lines wherever they are — the view, or the chart over a field.
        if (group.slot === "lines") {
          if (removable) void activateComposition({ fill: composition.fill, lines: null });
          else if (isPressureBundle(id)) void activateComposition({ ...composition, lines: id });
        } else {
          void activateComposition({ fill: id, lines: composition.lines });
        }
      });
      container.append(button);
    }
    levelRow.append(container);
  }
  // A row wider than the capsule scrolls: bring each pressed member into
  // view (the lines' last, so the group that changed most recently wins)
  // and fade whichever end is clipped.
  for (const pressed of levelRow.querySelectorAll<HTMLButtonElement>('button[aria-pressed="true"]')) {
    pressed.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  syncLevelRowFade();
}

/** How far the level row is clipped at each end, as the two lengths the
 * stylesheet's mask fades over; both zero for a row that fits. */
function syncLevelRowFade(): void {
  const clipped = levelRow.scrollWidth - levelRow.clientWidth;
  const start = clipped > 1 && levelRow.scrollLeft > 1 ? 28 : 0;
  const end = clipped > 1 && levelRow.scrollLeft < clipped - 1 ? 28 : 0;
  levelRow.style.setProperty("--fade-start", `${start}px`);
  levelRow.style.setProperty("--fade-end", `${end}px`);
}
levelRow.addEventListener("scroll", syncLevelRowFade, { passive: true });
window.addEventListener("resize", syncLevelRowFade);

/** Whether one of the shell's own rail tiles stands for a bundle: a tile
 * of its own, its family's, or the pressure family's for a surface it
 * charts. A registered family the shell writes no tile for (specific
 * humidity, which no source publishes) reads as unknown here, so a run
 * that does ship it still gets a way onto the screen. */
function railTileStandsFor(id: string): boolean {
  const family = familyOf(id as ForecastBundleId);
  return variableButtons().some(
    (button) =>
      !("unknown" in button.dataset) &&
      (button.dataset.variable === id ||
        (family !== null && button.dataset.family === family) ||
        (button.dataset.group === "pressure" && (PRESSURE_BUNDLE_IDS as readonly string[]).includes(id))),
  );
}

/** A generic tile's icon — stacked layers, since the shell knows nothing
 * of the quantity — drawn the way the written tiles' icons are. */
function unknownRailIcon(): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "rail-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M12 4l8.5 4.5L12 13 3.5 8.5z M3.5 12.5L12 17l8.5-4.5 M3.5 16.5L12 21l8.5-4.5");
  svg.append(path);
  return svg;
}

/** Give every bundle the run publishes a way onto the screen, including the
 * ones this build has no tile written for. The shell's own tiles stand for
 * the families they name; anything else gets a plain one in manifest order,
 * lettered with the id's initial. Rebuilt per run, so a dataset that ships
 * nothing unusual carries no extra chrome. */
function syncUnknownRailTiles(run: ForecastManifest): void {
  if (!variableRail) return;
  for (const stale of variableRail.querySelectorAll("button[data-unknown]")) stale.remove();
  for (const bundle of run.bundles) {
    const id = bundle.variable;
    if (railTileStandsFor(id)) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.variable = id;
    button.dataset.unknown = "";
    button.disabled = variableButtonsDisabled || switchingVariable;
    button.setAttribute("aria-pressed", "false");
    // The id is all the tooltip can say before the bundle is open.
    button.dataset.tip = id;
    button.append(unknownRailIcon());
    const glyph = document.createElement("b");
    glyph.className = "rail-glyph";
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = id.slice(0, 1).toUpperCase();
    const name = document.createElement("span");
    name.className = "rail-name";
    const code = document.createElement("span");
    code.textContent = id.toUpperCase();
    const gloss = document.createElement("small");
    gloss.textContent = t("varUnknownLayer");
    name.append(code, " ", gloss);
    button.append(glyph, name);
    button.addEventListener("click", () => {
      void activateComposition({ fill: id, lines: composition.lines });
    });
    variableRail.append(button);
  }
}

/** The layers whose legend bar is a hand-written gradient in the stylesheet
 * (`body[data-variable=…] .legend-bar`, and the default for temperature);
 * every other field reads its bar off the palette it is actually drawn
 * with. */
const STYLESHEET_LEGEND_IDS: ReadonlySet<string> = new Set(["tmp2m", "prate", "cref", "wind10m"]);

/** Past ten visible tiles the rail no longer fits a laptop screen at full
 * size, so the stylesheet's dense variant takes over (see `.variable-rail`).
 * Nothing about which tiles show changes — only their height and icon. */
const DENSE_RAIL_TILES = 10;

function syncRailDensity(): void {
  if (!variableRail) return;
  const visible = variableButtons().filter((button) => !button.hidden).length;
  if (visible > DENSE_RAIL_TILES) variableRail.dataset.dense = "";
  else delete variableRail.dataset.dense;
  syncRailFade();
}

/** How far the rail is clipped at each end, as the two lengths the
 * stylesheet's mask fades over, the way `syncLevelRowFade` does for the
 * level row; a column that fits its box is not marked clipped at all, so
 * the mask (which costs the tiles their backdrop blur) is only ever on a
 * rail that scrolls. */
function syncRailFade(): void {
  if (!variableRail) return;
  const clipped = variableRail.scrollHeight - variableRail.clientHeight;
  const start = clipped > 1 && variableRail.scrollTop > 1 ? 28 : 0;
  const end = clipped > 1 && variableRail.scrollTop < clipped - 1 ? 28 : 0;
  variableRail.style.setProperty("--fade-start", `${start}px`);
  variableRail.style.setProperty("--fade-end", `${end}px`);
  if (clipped > 1) variableRail.dataset.clipped = "";
  else delete variableRail.dataset.clipped;
}
variableRail?.addEventListener("scroll", syncRailFade, { passive: true });
// The rail's box ends where the capsule begins on a narrow screen, and the
// capsule is taller with a level row on it: the stylesheet reads its
// measured height back through --capsule-height. The rail's own size
// changes with the viewport and the tile set, and either can clip it.
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--capsule-height", `${Math.ceil(timelinePanel.offsetHeight)}px`);
  syncRailFade();
}).observe(timelinePanel);
if (variableRail) new ResizeObserver(syncRailFade).observe(variableRail);

/** The legend bar's gradient for a field whose key is not in the stylesheet:
 * the upper-air fills, the surface diagnostics, solar radiation and every
 * unrecognized field read theirs off the palette they are actually drawn
 * with, over the range the legend's ticks span. */
function legendGradientFor(session: VariableSession): string {
  const chartId = session.chartId;
  if (chartId !== null && STYLESHEET_LEGEND_IDS.has(chartId)) return "";
  if (session.vector) return legendGradient(vectorPalette(session));
  const variable = session.variable;
  if (variable.quantization.type !== "linear") return "";
  const { offset, scale, maximumCode } = variable.quantization;
  let from = 0;
  let to = maximumCode;
  // A registered field may read over less than its codebook — a windowed
  // temperature surface, a diagnostic whose ramp saturates before the
  // codebook ends; an unrecognized field's bar spans the codebook, which is
  // exactly what its legend's ticks say.
  const range = chartId !== null && session.identity ? scalarLegendRange(session.identity) : null;
  if (range) {
    const [low, high] = range;
    from = Math.max(0, Math.round((low - offset) / scale));
    to = Math.min(maximumCode, Math.round((high - offset) / scale));
  }
  return legendGradient(buildPalette(variable, session.identity), from, to);
}

/** The instrument-panel copy for a session: the registry's, keyed by what the
 * field is, and a generic card read off the file's own metadata when it is
 * nothing this build knows. The title and unit are then the encoder's own
 * label and unit, and the legend spans the codebook. */
function variableUi(session: VariableSession): VariableUi {
  const known = session.chartId === null ? undefined : VARIABLE_UI[session.chartId];
  if (known) return known;
  const variable = session.variable;
  const quantization = variable.quantization;
  const range: readonly [number, number] =
    quantization.type === "linear"
      ? [quantization.offset, quantization.offset + quantization.scale * quantization.maximumCode]
      : [0, quantization.maximum];
  return {
    code: session.id.toUpperCase(),
    title: [variable.label],
    bufferTitle: "Data buffer",
    label: variable.label,
    legend: rangeLegend(range, niceStep(range[1] - range[0])),
  };
}

function updateModelPresentation(): void {
  document.body.dataset.model = selectedModelId;
  modelEyebrow.textContent = MODEL_EYEBROW[selectedModelId];
  for (const button of modelButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.model === selectedModelId));
  }
}

function updateVariablePresentation(session: VariableSession): void {
  const ui = variableUi(session);
  const model = FORECAST_MODELS[selectedModelId];
  const pressure = pressureLevelForIdentity(session.identity) !== null;
  document.body.dataset.variable = session.id;
  document.body.classList.toggle("is-pressure", pressure);
  applyBasemapTheme();
  updateModelPresentation();
  variableCode.textContent = `${model.label} / ${ui.code}`;
  dataCardTitle.textContent = ui.bufferTitle;
  // A case is its own page — its title and summary are what a search result
  // or a shared link should say; every live view is the one page at `/`,
  // whose indexed title is whichever view rendered, so it names the kind of
  // thing this is ("forecast map") and not only the field — in the page's
  // language, unlike the English headline over the map.
  applyPageMeta(
    activeCase
      ? {
          path: `/?case=${encodeURIComponent(activeCase.id)}`,
          title: localizedText(activeCase.title, locale),
          description: localizedText(activeCase.summary, locale),
        }
      : {
          path: "/",
          title: t("pageTitleLive", {
            variable: ui.label,
            model: model.label,
            hours: String(Math.round(frameLeadSeconds(frameCount() - 1) / HOUR_SECONDS)),
          }),
          description: t("metaDescription"),
        },
  );
  // One line at display size: the title sits over the map, and a wrapped
  // serif headline there fights the data underneath it.
  variableTitle.textContent = ui.title.join(" ");
  legend.setAttribute("aria-label", t("legendAria", { label: ui.label }));
  legendUnit.textContent = session.variable.unit;
  legendLabels.replaceChildren(...ui.legend.map((label) => {
    const span = document.createElement("span");
    span.textContent = label;
    return span;
  }));
  legendBar.style.background = legendGradientFor(session);
  // Each family remembers the member last on screen, so its rail tile
  // reopens it.
  const lines = composition.lines;
  if (lines !== null) lastFamilyMember.set("hgt", lines);
  const fillFamily = composition.fill === null ? null : familyOf(composition.fill);
  if (composition.fill !== null && fillFamily !== null) lastFamilyMember.set(fillFamily, composition.fill);
  // The level row: the fill's surfaces when its family has several, and the
  // lines' whenever a surface is drawn — alone or over a field; the rail's
  // one pressure tile stands for the lines-alone view.
  renderLevelRow();
  // The particle overlay belongs to the vector fields, so its switch appears
  // with them.
  particlesToggle.hidden = !session.vector;
  for (const button of variableButtons()) {
    const family = button.dataset.family as IsobaricFamily | undefined;
    const pressed = button.dataset.group === "pressure"
      ? pressure
      : family !== undefined
        ? fillFamily === family
        : button.dataset.variable === composition.fill;
    button.setAttribute("aria-pressed", String(pressed));
    // A rail taller than its box scrolls, and the tile on screen belongs in
    // view — clear of the fade, which is what the rail's scroll padding is.
    if (pressed && !button.hidden) button.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  syncRailFade();
}

/** Hold the camera to a case's own region. A case is the whole dataset, so
 * zooming out past the point where it fills the viewport only adds empty map,
 * and panning off it leaves nothing to look at. Both limits depend on the
 * viewport, so a resize recomputes them — without moving the camera, because
 * mobile browsers fire resize every time their toolbar slides. */
function applyCaseCamera(showcaseCase: ShowcaseCase, recenter: boolean): void {
  const canvas = map.getCanvas();
  // Drop the standing limits first: they would otherwise constrain the very
  // camera the new ones are measured from.
  map.setMaxBounds(null);
  map.setMinZoom(0);
  const limits = caseCameraLimits(showcaseCase.bbox, {
    width: canvas.clientWidth,
    height: canvas.clientHeight,
  });
  if (recenter) map.jumpTo({ center: limits.center, zoom: limits.minZoom });
  map.setMinZoom(limits.minZoom);
  map.setMaxBounds(limits.bounds);
}

/** Fill the showcase banner: which event this is, and the way back to the
 * list. The run itself is already on the station panel's "model run" line. */
function updateCasePresentation(showcaseCase: ShowcaseCase): void {
  caseTitle.textContent = localizedText(showcaseCase.title, locale);
  caseSummary.textContent = localizedText(showcaseCase.summary, locale);
  const [west, south, east, north] = showcaseCase.bbox;
  caseRegion.textContent = `${formatDegrees(north, "NS")} ${formatDegrees(west, "EW")} → ${formatDegrees(south, "NS")} ${formatDegrees(east, "EW")}`;
  caseBanner.hidden = false;
}

/** A bbox corner, in the compact form the instrument panel uses. */
function formatDegrees(value: number, axis: "NS" | "EW"): string {
  const hemisphere = axis === "NS" ? (value >= 0 ? "N" : "S") : (value >= 0 ? "E" : "W");
  return `${Math.abs(value).toFixed(Math.abs(value) % 1 === 0 ? 0 : 1)}°${hemisphere}`;
}

/** Keep the address bar shareable: reflect the on-screen model and
 * composition into the query string (replaceState — switches are not history
 * entries). `type` names the primary; `lines` the surface over a filled
 * field, and nothing when the lines are the view. */
function syncUrl(): void {
  const variableId = compositionPrimary(composition);
  const base = activeCase
    ? searchForCaseVariable(variableId, window.location.search, activeCase.id)
    : searchForVariable(variableId, window.location.search, selectedModelId);
  const withLines = searchWithLines(base, composition.fill !== null ? composition.lines : null);
  // Only a chosen, switched-off overlay is written; on is the default and
  // says nothing, so an ordinary shared link stays as short as it was.
  const search = searchWithParticles(withLines, particlesEnabled || !particlesChosen);
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
  quiet = false,
): Promise<ArrayBuffer> {
  if (!quiet) say(preloadState, "receivingBundle");
  const response = await fetchImmutable(artifactUrl(descriptor.path, descriptor.crc32));
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
      if (quiet) continue;
      updateDownloadProgress(offset, total);
      say(loadStatus, "receivingBundlePercent", { percent: Math.round((offset / total) * 100) });
    }
  } else {
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.byteLength > total) throw new Error(t("bundleTooLong"));
    data.set(buffer, 0);
    offset = buffer.byteLength;
    crc = crc32Update(crc, buffer);
    if (!quiet) updateDownloadProgress(offset, total);
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

/** Open a channel and wait for its `ready`: boot, hand it the init message,
 * parse the metadata it answers with. Only the messages of that handshake are
 * routed here — a decoded plane cannot arrive before the session it belongs
 * to exists, because nothing has asked for one yet. */
function initializeChannel(
  channel: DecodeChannel,
  initMessage: unknown,
  transfer: Transferable[],
  sequence: number,
): Promise<{ worker: DecodeChannel; metadata: BundleMetadata; tiles: TileGeometry | null }> {
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
            resolve({
              worker: channel,
              metadata: parsed,
              tiles: parseTileGeometry(message.tileGeometry as Uint32Array | null | undefined),
            });
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
          break;
        }
        case "error": {
          const text = String(message.message ?? t("decodeFailed"));
          if (typeof message.requestId === "number") inflight.delete(message.requestId);
          else reject(new Error(text));
          break;
        }
      }
    };
  });
}

/** Route a session's channel to the session itself.
 *
 * Every message a decoder sends names a variable by its `numericId`, which is
 * a *file-local* handle: the encoder numbers a bundle's variables 1..n, so
 * the temperature fill and the pressure lines on top of it both answer about
 * "variable 1". There is no global map from that number to a session, and
 * there must not be — the binding is the closure, one handler per channel. */
function bindChannel(session: VariableSession, sequence: number): void {
  session.worker.onmessage = (event: MessageEvent) => {
    const message = event.data as Record<string, unknown>;
    if (sequence !== initializeSequence) return;
    switch (message.type) {
      case "progress":
      case "resident":
        handleStreamMessage(message as Parameters<typeof handleStreamMessage>[0]);
        break;
      case "frame":
        handleDecodedFrame(session, message as unknown as Parameters<typeof handleDecodedFrame>[1]);
        break;
      case "series":
        handleProbeSeries(session, message as unknown as Parameters<typeof handleProbeSeries>[1]);
        break;
      case "error": {
        const text = String(message.message ?? t("decodeFailed"));
        if (typeof message.requestId === "number") inflight.delete(message.requestId);
        showError(t("frameDecodeFailed", { message: text }));
        break;
      }
    }
  };
}

/** The variables to draw from a bundle whose parameter blocks say nothing
 * this build recognizes: the one named like the bundle, else simply the
 * first. A two-variable bundle that is not a component pair in the table is
 * not a vector field, so only its first variable is drawn. A bundle named by
 * the convention is still held to carrying the variable that name promises —
 * a file contradicting its own manifest entry is a broken build, not a new
 * layer. */
function fallbackVariables(variableId: ForecastBundleId, bundleMetadata: BundleMetadata): BundleVariable[] {
  const named = bundleMetadata.variables.find((item) => item.id === variableId);
  if (named) return [named];
  if (identityForBundleId(variableId) !== null) throw new Error(t("bundleMissingVariable", { id: variableId }));
  return [bundleMetadata.variables[0]!];
}

/** The manifest can only be read by the naming convention, and the file
 * itself is authoritative. Where they disagree the parameter block wins
 * silently for rendering — but a run publishing `tmp850` bytes under the name
 * `rh850` is a pipeline bug the console should say out loud. Diagnostics stay
 * English in both locales. */
function warnOnIdentityMismatch(variableId: ForecastBundleId, identity: VariableIdentity | null): void {
  const guess = identityForBundleId(variableId);
  if (guess === null || sameIdentity(guess, identity)) return;
  console.warn(
    `bundle "${variableId}" is named for ${guess.family}${guess.level ?? ""} but its parameter block says ` +
      (identity === null ? "something this build does not know" : `${identity.family}${identity.level ?? ""}`) +
      "; the file wins",
  );
}

/** Download and initialize one variable's bundle; resident sessions are
 * reused. An overlay loads quietly — the data card and the status line
 * describe the primary — and takes the half-resolution tier where one is
 * offered: contour lines are smoothed again in the shader, and nothing the
 * lines slot draws needs the full grid, so the bytes go to the field the
 * viewer is actually reading. `?res=full` still pins every session. */
function loadVariable(
  variableId: ForecastBundleId,
  sequence: number,
  role: "primary" | "overlay" = "primary",
): Promise<VariableSession> {
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
    const overlay = role === "overlay";
    const variant = pickBundleVariant(
      descriptor.variants,
      neededGridWidth(),
      slowConnection(),
      overlay && resolutionPreference !== "full" ? "half" : resolutionPreference,
    );
    const video = h264Enabled ? descriptor.video : undefined;
    // Opted in, the video path must still earn its bytes — prefer it only
    // when the stream is not larger than the bundle it replaces (lossless
    // H.264 wins that comparison for tmp2m but loses it for prate; the
    // manifest's byteLengths decide, so a future lossy tier flips this
    // automatically).
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
          overlay,
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
        const initBuffer = await downloadBundle(target, sequence, overlay);
        if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
        channel = spawnWorker();
        initMessage = { type: "init", buffer: initBuffer };
        transfer = [initBuffer];
        downloadedBytes = target.byteLength;
      }
      format = variant ? "Xue ½" : "Xue";
      totalBytes = target.byteLength;
    }

    if (!overlay) say(loadStatus, streaming ? "readingIndex" : "initializingDecoder");
    const {
      worker: sessionWorker,
      metadata: bundleMetadata,
      tiles,
    } = await initializeChannel(channel, initMessage, transfer, sequence);
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
    // What the bundle *is* comes from its own variables' GRIB2 parameter
    // blocks, not from the name the manifest filed it under: a vector bundle
    // is one whose two variables are a component pair in the table, and the
    // u/v order is the table's. A file this build cannot place renders its
    // first variable as a plain scalar.
    const derived = identifyBundle(bundleMetadata.variables);
    const sessionVariables = derived?.variables ?? fallbackVariables(variableId, bundleMetadata);
    const identity = derived?.identity ?? null;
    warnOnIdentityMismatch(variableId, identity);
    const session: VariableSession = {
      id: variableId,
      key: nextSessionKey++,
      worker: sessionWorker,
      metadata: bundleMetadata,
      identity,
      chartId: registeredBundleId(identity),
      vector: identity?.vector === true && sessionVariables.length >= 2,
      variable: sessionVariables[0]!,
      variables: sessionVariables,
      format,
      bytes: downloadedBytes,
      totalBytes,
      extraBytes,
      tiles,
      streaming,
      resident: !streaming,
      residentScope: "bundle",
      viewTiles: null,
      leadOffsets: null,
    };
    // Streaming progress may have raced ahead of session registration.
    const early = pendingStream.get(variableId);
    if (early) {
      pendingStream.delete(variableId);
      if (early.bytes > 0) session.bytes = Math.min(totalBytes, extraBytes + early.bytes);
      if (early.resident) {
        session.resident = true;
        session.residentScope = early.scope;
      }
    }
    sessions.set(variableId, session);
    bindChannel(session, sequence);
    return session;
  })();
  sessionLoads.set(variableId, load);
  return load.finally(() => sessionLoads.delete(variableId));
}

/** Half-widths in device pixels for the two weights a chart draws. Thin
 * enough that a 4 hPa surface analysis does not fill in over a low, heavy
 * enough that the emphasised lines read at a glance. */
const CONTOUR_WIDTH = 0.6;
const CONTOUR_EMPHASIS_WIDTH = 1.2;

/** Standard deviation, in grid cells, of the smoothing a plane gets before
 * it is contoured. Sea level pressure is stored in 1 hPa codes and drawn
 * every 4 hPa, so in a weak gradient one code spans several cells and the
 * raw contour is a staircase along their edges; two cells of Gaussian
 * (half a degree on the production grids) is enough to recover the smooth
 * field underneath without blunting a low. The heights are quantized finer
 * relative to their interval and need less, but one figure keeps every
 * level of the family reading the same way. */
const CONTOUR_SMOOTHING_CELLS = 2;

/** The contour settings for a variable, or null when it is a filled field.
 *
 * Which interval to draw follows the field's `(family, level)` identity;
 * `identity` is a session's, read off the parameter block, and defaults to
 * the naming-convention guess for a poster, which has metadata but no
 * session. The dequantization comes off the bundle's own codebook rather than
 * the level registry: the registry says what to draw, the file says what its
 * codes mean, and a run encoded at a different profile stays correct.
 * A logarithmic codebook has no contour reading at all. */
function contourStyleFor(
  variable: BundleVariable,
  identity: VariableIdentity | null = identityForBundleId(variable.id),
  overlay = false,
): ContourStyle | null {
  const level = pressureLevelForIdentity(identity);
  if (!level || variable.quantization.type !== "linear") return null;
  if (overlay) {
    // Lines over another field: no fill of their own, and one ink that reads
    // on every ground the fills use — near-white on the dark theme's slates,
    // the paper theme's ink on white — a shade lighter than the chart's so
    // the field underneath stays the subject.
    return {
      offset: variable.quantization.offset,
      scale: variable.quantization.scale,
      interval: level.contourInterval,
      emphasisInterval: level.emphasisInterval ?? 0,
      values: (level.emphasisContours ?? []).slice(0, MAX_NAMED_CONTOURS),
      lineWidth: CONTOUR_WIDTH,
      emphasisWidth: CONTOUR_EMPHASIS_WIDTH,
      lineColor: isDark ? [1, 1, 1, 0.85] : [0.11, 0.1, 0.09, 0.85],
      fillAlpha: 0,
      smoothing: CONTOUR_SMOOTHING_CELLS,
    };
  }
  return {
    offset: variable.quantization.offset,
    scale: variable.quantization.scale,
    interval: level.contourInterval,
    emphasisInterval: level.emphasisInterval ?? 0,
    values: (level.emphasisContours ?? []).slice(0, MAX_NAMED_CONTOURS),
    lineWidth: CONTOUR_WIDTH,
    emphasisWidth: CONTOUR_EMPHASIS_WIDTH,
    // Chart lines are drawn in the ground's opposite: near-white on the dark
    // ocean, the paper theme's ink on its chart stock.
    lineColor: isDark ? [0.94, 0.96, 1, 1] : [0.11, 0.1, 0.09, 1],
    // A low-saturation fill under the lines: enough to read a ridge from a
    // trough at a glance, faint enough that the lines stay the subject.
    fillAlpha: 0.45,
    smoothing: CONTOUR_SMOOTHING_CELLS,
  };
}

// -- contour labels ----------------------------------------------------------
//
// Values written on the isobars, and an H or an L on every closed high and
// low. The lines are the GPU's; the labels need geometry, which
// `isolines.ts` traces from the displayed plane in its own worker, and
// MapLibre's symbol layers place — along the line for a value, at a point
// for a center — with the same collision handling the basemap's names get.
// A halo in the ground's own tone opens a gap in the line under each value,
// which is how a chart has always made room for a label.

const LABEL_SOURCE = "pressure-labels";
const CONTOUR_LABEL_LAYER = "pressure-contour-labels";
const CENTER_LABEL_LAYER = "pressure-center-labels";
/** During playback the labels are recomputed at most this often: a line's
 * label sits at a fixed distance along it, so re-tracing every frame would
 * make every label crawl. Stopping, stepping and panning refresh at once. */
const LABELS_PLAYBACK_INTERVAL_MS = 1000;
/** How far beyond the viewport the trace extends, as a fraction of it, so a
 * small pan shows labels before the next trace lands. */
const LABELS_VIEW_MARGIN = 0.2;
/** A high or a low has to be the extreme within this radius, and stand out
 * from that window's rim by a whole contour interval — one closed line. */
const CENTER_RADIUS_DEGREES = 8;

/** Set once the style has loaded. `map.isStyleLoaded()` is not that: it
 * also waits for every tile in flight, so it is false after each pan, which
 * is exactly when the labels are asked to come back. */
let mapStyleReady = false;
let labelsWorker: Worker | null = null;
let labelsRequestId = 0;
let labelsInFlight = false;
/** A frame asked for while one is being traced; sent when that one lands. */
let labelsPending: DecodedFrame | null = null;
let labelsLastPlane: DecodedFrame | null = null;
let labelsLastAt = 0;
let labelsShown = false;

function ensureLabelsWorker(): Worker {
  if (!labelsWorker) {
    labelsWorker = new Worker(new URL("./labels.worker.ts", import.meta.url), { type: "module" });
    labelsWorker.onmessage = (event: MessageEvent<LabelsWorkerResponse>) => handleLabels(event.data);
  }
  return labelsWorker;
}

/** The two symbol layers over one GeoJSON source, added under the basemap's
 * own names so a city keeps its label over a contour's. */
function ensureLabelLayers(): GeoJSONSource | null {
  if (!mapStyleReady) return null;
  const existing = map.getSource(LABEL_SOURCE);
  if (existing) return existing as GeoJSONSource;
  map.addSource(LABEL_SOURCE, { type: "geojson", data: emptyLabels(), buffer: 128 });
  const before = (map.getStyle().layers ?? []).find((entry) => entry.type === "symbol")?.id;
  map.addLayer(
    {
      id: CONTOUR_LABEL_LAYER,
      type: "symbol",
      source: LABEL_SOURCE,
      filter: ["==", ["geometry-type"], "LineString"],
      layout: {
        "symbol-placement": "line",
        "symbol-spacing": 420,
        "text-field": ["get", "label"],
        "text-font": ["Noto Sans Regular"],
        "text-size": 11,
        "text-letter-spacing": 0.04,
        "text-max-angle": 25,
        "text-padding": 4,
        "text-rotation-alignment": "map",
        "text-pitch-alignment": "viewport",
      },
      paint: { "text-halo-width": 2.5, "text-halo-blur": 0.5 },
    },
    before,
  );
  map.addLayer(
    {
      id: CENTER_LABEL_LAYER,
      type: "symbol",
      source: LABEL_SOURCE,
      filter: ["==", ["geometry-type"], "Point"],
      layout: {
        "text-field": [
          "format",
          ["get", "letter"],
          { "font-scale": 1.7, "text-font": ["literal", ["Noto Sans Medium"]] },
          "\n",
          {},
          ["get", "label"],
          { "font-scale": 0.9 },
        ],
        "text-font": ["Noto Sans Regular"],
        "text-size": 12,
        "text-line-height": 1.05,
        "text-padding": 6,
      },
      paint: { "text-halo-width": 2 },
    },
    before,
  );
  applyLabelInk();
  return map.getSource(LABEL_SOURCE) as GeoJSONSource;
}

/** Ink for the labels: the ground's own ink, and a halo of the ground's
 * ocean tone rather than the basemap's paper, so it reads as a gap in the
 * line rather than a sticker on it. Over a filled field there is no one
 * tone to open a gap in — the halo would be a box of ocean on a warm
 * temperature field — so the lines slot as an overlay takes a soft shadow
 * in the ink's opposite instead, the way the basemap's own names do. */
function applyLabelInk(): void {
  if (!map.getLayer(CONTOUR_LABEL_LAYER)) return;
  const theme = currentBasemapTheme();
  const darkGround = document.body.dataset.ground === "dark";
  const overlay = slots.lines.session !== null && slots.lines.session !== activeSession;
  const ink = darkGround ? "#eef1f4" : "#2a2824";
  const halo = overlay ? (darkGround ? "rgba(0, 0, 0, 0.6)" : "rgba(255, 255, 255, 0.8)") : theme.ocean;
  for (const id of [CONTOUR_LABEL_LAYER, CENTER_LABEL_LAYER]) {
    map.setPaintProperty(id, "text-color", ink);
    map.setPaintProperty(id, "text-halo-color", halo);
    map.setPaintProperty(id, "text-halo-width", overlay ? 1.5 : id === CONTOUR_LABEL_LAYER ? 2.5 : 2);
    map.setPaintProperty(id, "text-halo-blur", overlay ? 1 : 0.5);
  }
}

function emptyLabels(): FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function clearLabels(): void {
  labelsPending = null;
  labelsLastPlane = null;
  // Anything still being traced is for a session that is gone.
  labelsRequestId += 1;
  labelsInFlight = false;
  if (labelsShown) {
    (map.getSource(LABEL_SOURCE) as GeoJSONSource | undefined)?.setData(emptyLabels());
    labelsShown = false;
  }
}

/** Trace the labels for the plane on screen: right away when the viewer is
 * stepping or panning, throttled during playback. */
function scheduleLabels(frame: DecodedFrame, force: boolean): void {
  const session = slots.lines.session;
  if (!session || !layersAdded || !contourStyleFor(session.variable, session.identity)) return;
  const now = performance.now();
  if (!force && playing && now - labelsLastAt < LABELS_PLAYBACK_INTERVAL_MS) {
    // Remember the newest frame so the next allowed trace is not stale.
    labelsPending = frame;
    return;
  }
  if (labelsInFlight) {
    labelsPending = frame;
    return;
  }
  sendLabels(frame);
}

/** The frame on screen, traced again — after a pan, a zoom, or a stop. */
function refreshLabels(): void {
  const slot = slots.lines;
  const session = slot.session;
  if (!session || slot.shownKey === null || !contourStyleFor(session.variable, session.identity)) return;
  const frame = cachedFrame(slot.shownKey, session);
  if (frame) scheduleLabels(frame, true);
}

/** The cells the view covers, widened by the margin, in the grid's own
 * terms: columns may run past the width on a wrapping grid. */
function viewportCellWindow(grid: ReturnType<typeof geoGrid>): CellWindow | null {
  const bounds = map.getBounds();
  const west = bounds.getWest();
  const east = bounds.getEast();
  const north = bounds.getNorth();
  const south = bounds.getSouth();
  const spanLongitude = Math.min(360, (east - west) * (1 + 2 * LABELS_VIEW_MARGIN));
  const spanLatitude = (north - south) * (1 + 2 * LABELS_VIEW_MARGIN);
  const centerLongitude = (west + east) / 2;
  const centerLatitude = (north + south) / 2;
  const columnSpan = Math.ceil(spanLongitude / grid.longitudeStep) + 1;
  const rowSpan = Math.ceil(spanLatitude / Math.abs(grid.latitudeStep)) + 1;
  let column0 = Math.floor((centerLongitude - spanLongitude / 2 - grid.firstLongitude) / grid.longitudeStep);
  let columns = columnSpan;
  if (grid.wraps) {
    column0 = wrap(column0, grid.width);
    columns = Math.min(columns, grid.width);
  } else {
    const end = Math.min(grid.width, column0 + columns);
    column0 = Math.max(0, column0);
    columns = end - column0;
  }
  // Rows count down from the first (northern) latitude.
  const rowStart = Math.floor((centerLatitude + spanLatitude / 2 - grid.firstLatitude) / grid.latitudeStep);
  const row0 = Math.max(0, rowStart);
  const rows = Math.min(grid.height, rowStart + rowSpan) - row0;
  if (columns <= 0 || rows <= 0) return null;
  return { column0, row0, columns, rows };
}

function sendLabels(frame: DecodedFrame): void {
  const session = slots.lines.session;
  if (!session) return;
  const style = contourStyleFor(session.variable, session.identity);
  const level = pressureLevelForIdentity(session.identity);
  if (!style || !level || !ensureLabelLayers()) return;
  const grid = geoGrid(session.metadata);
  const window = viewportCellWindow(grid);
  if (!window) return;
  const request: LabelRequest = {
    grid,
    window,
    coverage: sessionCoverage(session, frame.tiles),
    stride: strideFor(window.columns * window.rows),
    smoothingCells: style.smoothing,
    offset: style.offset,
    scale: style.scale,
    interval: level.contourInterval,
    centerRadiusDegrees: CENTER_RADIUS_DEGREES,
    prominence: level.contourInterval,
  };
  labelsRequestId += 1;
  labelsInFlight = true;
  labelsLastPlane = frame;
  labelsLastAt = performance.now();
  // The cache keeps the plane; the worker gets its own copy to read.
  const copy = frame.plane.slice();
  const message: LabelsWorkerRequest = { requestId: labelsRequestId, buffer: copy.buffer, request };
  ensureLabelsWorker().postMessage(message, [copy.buffer]);
}

function handleLabels(response: LabelsWorkerResponse): void {
  if (response.requestId !== labelsRequestId) return;
  labelsInFlight = false;
  const source = ensureLabelLayers();
  if (source) {
    const features: Feature[] = [];
    for (const line of response.result.lines) {
      features.push({
        type: "Feature",
        properties: { label: String(line.value) },
        geometry: { type: "LineString", coordinates: line.coordinates },
      });
    }
    for (const center of response.result.centers) {
      features.push({
        type: "Feature",
        properties: {
          letter: t(center.kind === "high" ? "centerHigh" : "centerLow"),
          label: String(center.value),
        },
        geometry: { type: "Point", coordinates: [center.longitude, center.latitude] },
      });
    }
    source.setData({ type: "FeatureCollection", features });
    labelsShown = true;
  }
  // A frame that arrived during the trace, unless it is the one just done.
  const pending = labelsPending;
  labelsPending = null;
  if (pending && pending !== labelsLastPlane) scheduleLabels(pending, !playing);
}

/** Add the slot layers to the map on first use, fill under lines, both under
 * the basemap's boundaries and names; the particles, created later, go over
 * both. Grid configuration is the caller's business — poster and bundle
 * planes use different grids. */
function ensureLayers(): void {
  if (layersAdded) return;
  map.addLayer(slots.fill.layer, FORECAST_ANCHOR_LAYER);
  map.addLayer(slots.lines.layer, FORECAST_ANCHOR_LAYER);
  layersAdded = true;
}

/** Empty a slot: nothing drawn, no session, and nothing waited on. The
 * session itself stays resident, like every session that leaves the screen. */
function detachSlot(slot: RasterSlot): void {
  slot.session = null;
  slot.displayedReal = null;
  slot.shownKey = null;
  slot.wantedKey = null;
  slot.layer.setVisible(false);
}

/** Point a slot's layer at a session: its palette, its contour style (the
 * chart's own when the lines are the view, the overlay's over a field), and
 * magnitude mode for wind. */
function configureSlotLayer(slot: RasterSlot, session: VariableSession, overlay: boolean): void {
  slot.session = session;
  const { layer } = slot;
  if (session.vector) {
    // A vector field is a filled field like every other layer — the
    // magnitude, colored through the same shader from the u/v pair in one
    // pass — and the particles ride over it as an optional overlay.
    const field = windVectorField(session);
    layer.setContours(null);
    layer.setVectorField(field);
    layer.setPalette(vectorPalette(session));
    // Without linear codebooks on both components there is no speed to
    // color; the overlay is then the whole layer, as it used to be.
    layer.setVisible(field !== null);
  } else {
    layer.setVectorField(null);
    layer.setPalette(buildPalette(session.variable, session.identity));
    layer.setContours(contourStyleFor(session.variable, session.identity, overlay));
    // An overlay shows nothing until its first plane lands: the slot may
    // still hold another surface's plane, and lines of the wrong level over
    // the field would be worse than none.
    layer.setVisible(!overlay);
  }
}

/** The ceiling a vector session's magnitude palette tops out at, from what
 * the field is rather than what it is called. */
function sessionMaxMagnitude(session: VariableSession): number {
  const identity = session.identity;
  return identity === null ? vectorMaxMagnitude("wind", null) : vectorMaxMagnitude(identity.family, identity.level);
}

/** The magnitude palette of a vector session: the wind ramp up to the
 * field's own ceiling, or the vapour flux ramp. */
function vectorPalette(session: VariableSession): Uint8Array {
  const max = sessionMaxMagnitude(session);
  return session.identity?.family === "qflux" ? buildVapourFluxPalette(max) : buildWindFieldPalette(max);
}

/** A vector bundle's own decode for the layer's magnitude mode: each
 * component's linear codebook, plus the magnitude the palette tops out at.
 * Null when either component is not linearly quantized — nothing published
 * is, and a magnitude has no meaning without both. */
function windVectorField(session: VariableSession): VectorField | null {
  const [u, v] = session.variables;
  if (u?.quantization.type !== "linear" || v?.quantization.type !== "linear") return null;
  // One reserved code serves both channels; the encoder gives the pair the
  // same codebook, and a file that did not could not be drawn as one field.
  if (u.quantization.nodataCode !== v.quantization.nodataCode) return null;
  return {
    offset: [u.quantization.offset, v.quantization.offset],
    scale: [u.quantization.scale, v.quantization.scale],
    nodataCode: u.quantization.nodataCode,
    maxMagnitude: sessionMaxMagnitude(session),
  };
}

/** Turn the particle overlay on or off. Never a reload: the speed field on
 * screen is the same either way, so this is a visibility flip plus the tiles
 * the session should now be fetching — off, wind narrows to the viewport like
 * any other streaming scalar; on, it needs the whole grid again because the
 * particles respawn across all of it. */
function setParticlesEnabled(next: boolean): void {
  if (particlesEnabled === next) return;
  particlesEnabled = next;
  particlesChosen = true;
  particlesToggle.setAttribute("aria-pressed", String(next));
  try {
    localStorage.setItem(PARTICLES_KEY, next ? "1" : "0");
  } catch {
    // The URL below still carries the choice for this session.
  }
  syncUrl();
  const session = activeSession;
  if (!session || !session.vector) return;
  if (next) {
    ensureWindLayer();
    ensureWindGrid(session);
  }
  windLayer?.setVisible(next);
  refreshViewportTiles();
  // refreshViewportTiles only re-selects when the tile set actually changed
  // (a downloaded bundle narrows nothing), and an overlay just switched on
  // still has to be handed the planes it was not being given.
  const target = requestedFrameIndex ?? activeFrameIndex;
  if (target !== null) trySelectFrame(target);
}

/** Create the wind particle layer on first use, above the scalar plane and
 * below the boundary lines. */
function ensureWindLayer(): WindParticleLayer {
  if (!windLayer) {
    windLayer = new WindParticleLayer((message) => showError(message));
    windLayer.animate = !reducedMotion.matches;
    // The field below already colors speed; a second ramp on top of it would
    // read as mud, so the particles are drawn in one ink.
    windLayer.setInk(particleInk());
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
    const slot = slotFor(variableId);
    // A real frame of this variable beat the poster to the screen.
    if (slot.displayedReal === variableId) return;
    const posterMetadata = parseBundleMetadata(descriptor.metadataJson);
    const variable = posterMetadata.variables.find((item) => item.id === variableId);
    if (!variable) return;
    ensureLayers();
    const target = slot.layer;
    target.configureGrid(posterMetadata);
    slot.gridSource = posterMetadata;
    slot.displayedReal = null;
    slot.shownKey = null;
    // Every poster is one scalar plane, even the wind bundle's: switching
    // away from wind has to leave magnitude mode before this uploads.
    target.setVectorField(null);
    target.setPalette(buildPalette(variable));
    target.setContours(contourStyleFor(variable));
    target.setFrame(plane);
    target.setVisible(true);
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
  const previousLead = previous
    ? frameOffsets(previous).map((offset) => offset * axisUnitSeconds(previous))
    : null;
  metadata = session.metadata;
  const time = metadata.time;
  if (previous && sameTimeAxis(previous, time)) {
    return;
  }
  // Short axes play too fast at the default rate; the opening speed follows
  // the loop's real length until the viewer overrides it.
  if (!playbackFpsChosen) setPlaybackFps(defaultFpsForLoop(loopUnits()), false);
  const previousIndex = activeFrameIndex ?? Number(slider.value);
  const firstLead = frameOffsets(time)[0]! * axisUnitSeconds(time);
  const lead = previousLead
    ? (previousLead[Math.min(previousIndex, previousLead.length - 1)] ?? firstLead)
    : firstLead;
  const index = nearestFrameIndex(lead);
  slider.max = String(time.frameCount - 1);
  slider.value = String(index);
  activeFrameIndex = null;
  requestedFrameIndex = null;
  trackHorizon.textContent = `+${Math.round((frameOffsets(time).at(-1)! * axisUnitSeconds(time)) / HOUR_SECONDS)}H`;
  buildTicks(time.frameCount);
  buildForecastDays();
  dataCardIndex.textContent = `${time.frameCount}F`;
  buildPreloadSegments(time.frameCount);
}

function applyVariable(session: VariableSession): void {
  if (!layersAdded) return;
  // Another level's labels, or a filled field's none, replace the last.
  clearLabels();
  activeSession = session;
  activeVariable = session.variable;
  selectedVariableId = session.id;
  // The composition follows the session that actually landed: a slot the
  // run could not fill has already emptied by now.
  composition = compositionForPrimary(session.id, composition.lines);
  syncTimeline(session);
  const slot = slotFor(session.id);
  configureSlotLayer(slot, session, false);
  // The fill slot is only ever primary: with a surface as the view it goes
  // dark. The lines slot is reconciled against the composition below.
  if (slot !== slots.fill) detachSlot(slots.fill);
  const wind = session.vector;
  if (wind && particlesEnabled) {
    ensureWindLayer();
    ensureWindGrid(session);
  }
  windLayer?.setVisible(wind && particlesEnabled);
  applyOverlays();
  updateVariablePresentation(session);
  syncUrl();
  updateCacheReadout();
  // The data card reflects only the variable on screen: its own delivery
  // format, its own downloaded bytes, and its own delivery state — never a
  // cross-variable total.
  preloadFormat.value = session.format;
  refreshDataCard(session);
  // Sessions differ in grid, tiling and delivery, so the view's tiles are the
  // new session's to answer.
  refreshViewportTiles();
  const index = activeFrameIndex ?? Number(slider.value);
  trySelectFrame(index);
  // The other variable has its own codebook, unit and grid, and its own
  // samples in the frame cache — and, on a tiled bundle, its own series to
  // read for the pinned cell.
  if (probe) {
    seedProbeFromCache();
    requestProbeSeries();
    scheduleProbeRender();
  }
}

/** Reconcile the lines slot with the composition: over a filled field it
 * carries the surface named in `lines`, loading it if need be — after the
 * primary, never blocking it — and it empties when the composition names
 * none. With the lines as the view the slot is the primary's and left alone. */
function applyOverlays(): void {
  const slot = slots.lines;
  if (slot === primarySlot()) return;
  const wanted = composition.fill !== null ? composition.lines : null;
  if (wanted === null) {
    // The lines go, and their labels with them: the label source is fed by
    // the slot's frames, so nothing else would ever empty it.
    if (slot.session) {
      detachSlot(slot);
      clearLabels();
    }
    return;
  }
  if (slot.session?.id === wanted) {
    // Already here — but possibly as the view, with the chart's own style.
    configureSlotLayer(slot, slot.session, true);
    if (slot.shownKey !== null) slot.layer.setVisible(true);
    return;
  }
  if (slot.session) detachSlot(slot);
  const sequence = initializeSequence;
  loadVariable(wanted, sequence, "overlay")
    .then((session) => {
      if (sequence !== initializeSequence || !ready) return;
      // The composition may have moved on while this loaded.
      if (composition.fill === null || composition.lines !== wanted) return;
      attachOverlay(slot, session);
    })
    .catch((error: unknown) => {
      if (sequence !== initializeSequence) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      showError(error instanceof Error ? error.message : t("bundleLoadFailed"));
    });
}

/** Put a loaded session in an overlay slot and hand it the frame on screen. */
function attachOverlay(slot: RasterSlot, session: VariableSession): void {
  configureSlotLayer(slot, session, true);
  clearLabels();
  applyLabelInk();
  refreshViewportTiles();
  const index = activeFrameIndex ?? requestedFrameIndex ?? Number(slider.value);
  sendPrefetchWindow(index);
  if (activeFrameIndex !== null) trySelectOverlayFrame(slot, activeFrameIndex);
  else requestOverlayDecode(slot, index);
}

/** Switch to a composition: load its primary if it is not the one on screen
 * (the existing variable switch), then reconcile the overlays. A slot the
 * run does not ship empties rather than errors. */
async function activateComposition(next: ViewComposition): Promise<void> {
  if (!manifest || !layersAdded || switchingVariable) return;
  const resolved = resolveComposition(next, manifest, activeCase?.defaultVariable ?? DEFAULT_VARIABLE);
  const primary = compositionPrimary(resolved);
  composition = resolved;
  if (primary !== activeSession?.id) {
    await activateVariable(primary);
    return;
  }
  // The same primary, other lines: only the overlay changes.
  applyOverlays();
  if (activeSession) {
    updateVariablePresentation(activeSession);
    syncUrl();
  }
}

async function activateVariable(variableId: ForecastBundleId): Promise<void> {
  if (!manifest || !layersAdded || switchingVariable || variableId === activeSession?.id) return;
  const sequence = initializeSequence;
  const resident = sessions.get(variableId);
  if (resident) {
    applyVariable(resident);
    // Frame counts can differ per variable (ECMWF prate has no analysis
    // frame), so the readiness line follows the session it now describes.
    say(loadStatus, "framesReady", { count: frameCount() });
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
  say(loadStatus, "readingData");
  loadStatus.className = "load-status is-loading";
  selectedVariableId = variableId;
  void showPoster(variableId, sequence);
  try {
    const session = await loadVariable(variableId, sequence);
    if (sequence !== initializeSequence) return;
    applyVariable(session);
    say(loadStatus, "framesReady", { count: frameCount() });
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
  vectorPlanes.clear();
  probe?.clear();
  probeSeriesRequests.clear();
  scheduleProbeRender();
  lastDecodeMs = null;
  decodeEvents.length = 0;
  lastPrefetchWindow.clear();
  pendingStream.clear();
  for (const session of sessions.values()) session.worker.terminate();
  sessions.clear();
  sessionLoads.clear();
  manifest = null;
  manifestUrl = null;
  currentRun = null;
  metadata = null;
  for (const slot of [slots.fill, slots.lines]) {
    detachSlot(slot);
    slot.gridSource = null;
  }
  windLayerGridSource = null;
  windLayer?.setVisible(false);
  particlesToggle.hidden = true;
  clearLabels();
  activeSession = null;
  activeVariable = null;
  activeFrameIndex = null;
  requestedFrameIndex = null;
  slider.value = "0";
  slider.disabled = true;
  playButton.disabled = true;
  speedButton.disabled = true;
  setVariableButtonsDisabled(true);
  document.body.classList.add("is-data-loading");
  dataCard.setAttribute("aria-busy", "true");
  hideError();
  resetPreloadCard(FRAME_COUNT);
  updateModelPresentation();
  say(loadStatus, "readingData");
  loadStatus.className = "load-status is-loading";
  say(runTime, "awaitingData");

  try {
    // Two ways in, one manifest contract. The live feed reads the mutable
    // pointer for the current run; a showcase case reads the mutable catalog
    // for its fixed historical one. Either way the manifest and everything
    // below it are immutable and cached via ?v=<crc32>.
    let loadedManifest: ForecastManifest;
    if (requestedCaseId !== null) {
      const catalog = await fetchCatalog(dataBaseUrl());
      if (sequence !== initializeSequence) return;
      const found = catalog.cases.find((item) => item.id === requestedCaseId);
      if (!found) throw new Error(t("showcaseCaseMissing", { id: requestedCaseId }));
      activeCase = found;
      selectedModelId = found.modelId;
      // The case names the layer its event is about — a heat dome opens on
      // temperature, not on the app's usual precipitation. Only an explicit
      // ?type= overrides it.
      if (!caseDefaultApplied && requestedVariableId === null) {
        composition = compositionForPrimary(found.defaultVariable, composition.lines);
      }
      caseDefaultApplied = true;
      document.body.classList.add("is-showcase");
      updateCasePresentation(found);
      // Frame the event before any byte lands, so the first painted plane
      // arrives on the region it belongs to rather than on the world view.
      applyCaseCamera(found, true);
      const loadedCase = await fetchCaseManifest(dataBaseUrl(), found);
      if (sequence !== initializeSequence) return;
      loadedManifest = loadedCase.manifest;
      manifestUrl = loadedCase.manifestUrl;
      currentRun = found.run;
    } else {
      const loaded = await fetchManifest(dataBaseUrl(), selectedModelId);
      if (sequence !== initializeSequence) return;
      loadedManifest = loaded.manifest;
      manifestUrl = loaded.manifestUrl;
      currentRun = loaded.latest.run;
    }
    manifest = loadedManifest;
    // The dataset is settled here (a case pins its own), so the timeline can
    // be titled for what it actually shows.
    applyDatasetWording();
    sayText(runTime, formatDate(loadedManifest.runTime));

    // Each variable button appears only when the manifest actually ships its
    // bundle. On the live feed that is wind10m everywhere and dswrf on the
    // sflux source; a showcase case additionally ships only the variables
    // its event is about, so the core pair can be missing too.
    for (const button of variableButtons()) {
      const bundleId = button.dataset.variable;
      if (!bundleId || !(KNOWN_BUNDLE_IDS as readonly string[]).includes(bundleId)) continue;
      const family = button.dataset.family as IsobaricFamily | undefined;
      button.hidden =
        button.dataset.group === "pressure"
          ? !PRESSURE_BUNDLE_IDS.some((id) => hasBundle(loadedManifest, id))
          : family !== undefined
            ? !familyMembers(family).some((id) => hasBundle(loadedManifest, id))
            : !hasBundle(loadedManifest, bundleId);
    }
    syncUnknownRailTiles(loadedManifest);
    syncRailDensity();
    // A slot this run does not ship empties; a case names its own default
    // for when that leaves nothing, and a live run always carries the core
    // pair.
    composition = resolveComposition(composition, loadedManifest, activeCase?.defaultVariable ?? DEFAULT_VARIABLE);
    selectedVariableId = compositionPrimary(composition);

    // Paint the poster while the bundle opens (never blocks the load).
    void showPoster(selectedVariableId, sequence);

    const session = await loadVariable(selectedVariableId, sequence);
    if (sequence !== initializeSequence) return;

    ensureLayers();
    // If a poster is on screen, keep it: ensureSlotGrid() switches the
    // layer to the session's bundle grid the moment the first real plane is
    // ready (trySelectFrame's cached branch).

    ready = true;
    // applyVariable adopts the session's time axis (syncTimeline) and builds
    // the slider, ticks, and day strip from it.
    applyVariable(session);

    say(loadStatus, "framesReady", { count: frameCount() });
    loadStatus.className = "load-status";
    slider.disabled = false;
    playButton.disabled = false;
    speedButton.disabled = false;
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
for (const button of variableButtons()) {
  button.addEventListener("click", () => {
    const id = button.dataset.variable;
    if (!isBundleVariableId(id)) return;
    const family = button.dataset.family as IsobaricFamily | undefined;
    if (button.dataset.group === "pressure") {
      // The pressure tile is the lines-alone view; from a field with lines
      // over it, this is the way out to the chart itself.
      void activateComposition({ fill: null, lines: preferredPressureVariable() });
    } else if (family !== undefined) {
      // A family tile opens the member last on screen; the level row then
      // moves between its surfaces. Lines over it stay.
      void activateComposition({ fill: preferredFamilyMember(family), lines: composition.lines });
    } else {
      // A fill tile changes the field alone; lines over it stay.
      void activateComposition({ fill: id, lines: composition.lines });
    }
  });
}
for (const button of modelButtons) {
  button.addEventListener("click", () => {
    const modelId = button.dataset.model;
    modelSheetControl.close();
    if (!modelId || !FORECAST_MODEL_IDS.includes(modelId as ForecastModelId)) return;
    if (activeCase || modelId === selectedModelId || switchingVariable) return;
    // A model is a separate dataset (own pointer, own run, own time axis), so
    // switching tears the whole session state down and re-tunes, keeping the
    // currently selected variable.
    selectedModelId = modelId as ForecastModelId;
    updateModelPresentation();
    syncUrl();
    void initialize();
  });
}
retryButton.addEventListener("click", () => void initialize());

/** Everything the theme touches that is not the stylesheet's to repaint:
 * the basemap flavor, the ground tones and inks over it, the contour ink
 * of whatever surface is drawn, the particle ink, and the two canvases that
 * read their colors off the chrome's tokens. The session — its workers,
 * its decoded frames, its playhead — is untouched. */
function applyAppearance(): void {
  syncBasemapStyle();
  windLayer?.setInk(particleInk());
  for (const slot of [slots.fill, slots.lines]) {
    const session = slot.session;
    if (!session || session.vector) continue;
    slot.layer.setContours(contourStyleFor(session.variable, session.identity, session !== activeSession));
  }
  scheduleProbeRender();
}

/** Everything the language touches beyond the static markup (which
 * `setLocale` has already rewritten): the copy this module composes from
 * state — the instrument table, the timeline wording, the day marks, the
 * level row and legend, the page's own metadata, the long-lived status
 * lines — and the basemap's label language. */
function applyLocale(): void {
  VARIABLE_UI = buildVariableUi();
  renderLanguageList();
  probePanel.root.setAttribute("aria-label", t("probeAria"));
  applyDatasetWording();
  updateTransport();
  resayAll();
  if (variableRail) {
    for (const gloss of variableRail.querySelectorAll("button[data-unknown] small")) gloss.textContent = t("varUnknownLayer");
  }
  const index = activeFrameIndex ?? Number(slider.value);
  if (metadata) {
    buildForecastDays();
    updateForecastDay(index);
  }
  if (activeCase) updateCasePresentation(activeCase);
  if (activeSession) updateVariablePresentation(activeSession);
  else applyPageMeta({ path: activeCase ? `/?case=${encodeURIComponent(activeCase.id)}` : "/" });
  renderProbe();
  syncBasemapStyle();
  // The H/L letters on the pressure centers are the dictionary's.
  refreshLabels();
}

// Neither the locale nor the theme reloads: the picker and the toggle each
// persist the choice and repaint in place through the two listeners here.
// The language picker is wired where it is built, beside the other sheets.
onThemeChange(applyAppearance);
onLocaleChange(applyLocale);
required<HTMLButtonElement>("theme-toggle").addEventListener("click", toggleTheme);
// Right-click (long-press on touch) over the map opens the custom menu:
// 「详细统计信息」 pins the stats card, 「复制调试信息」 copies a plain-text
// snapshot. The map-level event (not a DOM listener) is what makes this
// coexist with right-drag rotate: MapLibre suppresses the native menu for map
// listeners and skips firing after a rotate drag.
map.on("contextmenu", (event) => {
  showContextMenu(event.originalEvent.clientX, event.originalEvent.clientY);
});
// A left click pins the point probe. MapLibre fires this only for a real
// click on the map (a drag that ends on the canvas does not), and never for
// clicks on the panels above it.
map.on("click", (event) => {
  hideContextMenu();
  setProbe(event.lngLat.lng, event.lngLat.lat);
});
window.addEventListener("pointerdown", (event) => {
  if (!contextMenu.hidden && !contextMenu.contains(event.target as Node)) hideContextMenu();
});
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  // The sheets close themselves on Escape (createSheet wires that); this
  // handler carries the surfaces that have no sheet of their own.
  hideContextMenu();
  closeProbe();
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
map.on("moveend", () => {
  refreshViewportTiles();
  updateStatsReadout();
  refreshLabels();
});
window.addEventListener("resize", () => updateStatsReadout());
playButton.addEventListener("click", () => {
  if (playing) stopPlayback();
  else startPlayback();
});
// One button cycling the ladder: at four rungs a menu would cost more taps
// than it saves, and the label always reads the rate in force.
speedButton.addEventListener("click", () => setPlaybackFps(nextFps(playbackFps), true));
particlesToggle.addEventListener("click", () => setParticlesEnabled(!particlesEnabled));
/** Poll the live pointer; a changed run id re-initializes onto the new
 * run ("排播型电视直播" — the client tunes itself to the newest broadcast). */
async function checkForNewRun(): Promise<void> {
  // A case is a fixed historical run; there is no newer one to move to.
  if (activeCase || !currentRun || document.hidden || switchingVariable) return;
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
// A narrower window fits less of the case, so its limits move with it.
map.on("resize", () => {
  if (activeCase) applyCaseCamera(activeCase, false);
});

reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches) stopPlayback();
  if (windLayer) windLayer.animate = !reducedMotion.matches;
});

// A chosen playback rate sticks across visits; without one each dataset
// opens at the speed its loop length calls for.
try {
  const stored = parseStoredFps(localStorage.getItem(PLAYBACK_FPS_KEY));
  if (stored !== null) setPlaybackFps(stored, true);
} catch {
  // Storage can be unavailable (privacy modes); the default rate applies.
}

// Stats visibility sticks across visits, like the playback rate.
try {
  if (localStorage.getItem(STATS_VISIBLE_KEY) === "1") setStatsVisible(true);
} catch {
  // Panel just starts hidden.
}

updateTransport();
particlesToggle.setAttribute("aria-pressed", String(particlesEnabled));
buildTicks(FRAME_COUNT);
resetPreloadCard(FRAME_COUNT);
map.once("load", () => {
  mapStyleReady = true;
  // A theme or language picked while the style was still loading was built
  // into nothing; the sync is a no-op otherwise, and applies the ground.
  syncBasemapStyle();
  void initialize();
});
