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
  Marker,
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
  DERIVED_MAX_CODE,
  frontPalette,
  inflowPalette,
  steppedPrecipitationLegend,
  steppedPrecipitationPalette,
  thermalFrontZone,
  warmMoistInflow,
} from "./composite";
import {
  FORECAST_MODEL_IDS,
  FORECAST_MODELS,
  containerOf,
  deliveryBytes,
  fetchLatestPointer,
  fetchManifest,
  ManifestRejectedError,
  hasBundle,
  isBundleVariableId,
  modelDefaultVariable,
  modelRailCore,
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
  bundleLevel,
  familyLabel,
  familyLevels,
  familyMembers,
  familyOf,
  familyVariants,
  isobaricCode,
  levelCode,
  niceStep,
  rangeLegend,
  scalarLegendRange,
  vectorMaxMagnitude,
  type IsobaricFamily,
  UNTILED_BUNDLE_IDS,
} from "./levels";
import { buildPalette, buildVapourFluxPalette, buildWaveFieldPalette, buildWindFieldPalette, legendGradient } from "./palettes";
import {
  PRESSURE_BUNDLE_IDS,
  isPressureBundle,
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
  parseBackendFromSearch,
  parseCameraFromHash,
  parseCaseFromSearch,
  parseExperimentFromSearch,
  parseModelFromSearch,
  parseResolutionFromSearch,
  parseUseH264FromSearch,
  type StationsUrlState,
} from "./urlstate";
import {
  compositionForPrimary,
  compositionPrimary,
  parseView,
  searchForView,
  type ViewComposition,
  type ViewState,
} from "./viewstate";
import { FIELD_GROUPS, variableIds, variableSpec, type FieldGroup, type GroundId } from "./variables";
import { TC_MODELS } from "./tc/agencies";
import { buildTcCard } from "./tc/card";
import { pointDataOf, StormLayers, TC_CLICKABLE_LAYERS, type StormView, type TcPointData } from "./tc/layers";
import { renderTcPanel } from "./tc/panel";
import type { TcIndexEntry, TcStorm } from "./tc/schema";
import { fetchTcIndex, fetchTcStorm, resolveTcStormId, stormBounds, type LoadedTcIndex } from "./tc/tracks";
import { buildAirportCard, buildSoundingCard } from "./stations/card";
import {
  fetchAirportIndex,
  fetchAirportStation,
  fetchSoundingIndex,
  type LoadedAirportIndex,
  type LoadedSoundingIndex,
} from "./stations/fetch";
import {
  StationLayers,
  STATION_CLICKABLE_LAYERS,
  categoryColor,
  stationDataOf,
  type StationPointData,
} from "./stations/layers";
import { nearestAirport, nearestSounding } from "./stations/nearest";
import {
  nearestObservation,
  rowObservations,
  rowObservedValue,
  tafBands,
  type ObservationAxis,
} from "./stations/observations";
import type { AirportStation, AirportStationHistory } from "./stations/schema";
import { createSoundingSection, type SoundingSection } from "./sounding/section";
import { modelProfileBundles, nearestFrameForTime } from "./sounding/model";
import { profileFromModel, type ModelLevel, type Profile } from "./sounding/profile";
import { WindParticleLayer } from "./particles";
import { domainContains, lambertCone, regionShareOfView, type LambertDomain } from "./domain";
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
import {
  alignSeries,
  drawMeteogram,
  frameIndexAtX,
  meteogramRowCode,
  meteogramRows,
  seriesState,
  TAF_ROW_SPEC,
  type DayMark,
  type MeteogramRowData,
  type MeteogramRowSpec,
} from "./meteogram";
import { zoomCeilingForStep } from "./mercator";
import { displayUnit, displayValue } from "./units";
import { fetchPoster, isPosterSupported } from "./poster";
import { frameCacheKey, parseFrameCacheKey, variableKey } from "./sessionkeys";
import { applyTheme, isDark, onThemeChange, toggleTheme } from "./theme";
import {
  displayZone,
  formatClockStamp,
  formatCompactStamp,
  formatDayMark as formatDayMarkIn,
  formatStamp,
  onDisplayZoneChange,
  setDisplayZone,
  zoneAt,
  zoneDisplayName,
} from "./timezone";
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
import { spawnZarrWorker, zarrInitMessage, zarrObjectUrl, zarrRootUrl, zarrStoreFor } from "./zarr/channel";

// Rewrite the static shell into the detected locale and appearance before
// anything renders.
applyStaticMessages();
applyTheme();

/** The experimental synoptic composite (`?x=true`, urlstate.ts): the
 * precipitation on a stepped key, the sea level pressure
 * lines and their H/L marks over it, the particles run by the 850 hPa
 * vapour flux instead of the fill's own vector, and two fields the frontend
 * computes from several bundles at once (composite.ts) — the warm moist
 * inflow and the frontal zone. Opt-in and unremembered: it is a look at how
 * far the published data goes, not a product. */
const experiment = parseExperimentFromSearch(window.location.search);
const experimentEnabled = experiment.enabled;
/** What the experiment opens when the URL names nothing else. */
const EXPERIMENT_FILL: ForecastBundleId = "prate";
const EXPERIMENT_LINES: PressureBundleId = "prmsl";
/** The bundles the experiment's derived fields are computed from: the
 * vapour flux pair (whose particles also run over the fill) and the θe. */
const EXPERIMENT_FLOW_ID: ForecastBundleId = "qflux850";
const EXPERIMENT_WARMTH_ID: ForecastBundleId = "thetae850";

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
/** How often the live pointer is re-checked for a new run: a forecast
 * cycle lands every hour at most, an observation window is rebuilt every
 * five minutes, and the pointer is a 200-byte no-cache object, so the
 * observation feed is polled more often than the forecasts (the storm
 * product keeps the forecasts' cadence). */
const LATEST_POLL_MS = 5 * 60_000;
const OBSERVATION_POLL_MS = 2 * 60_000;
function latestPollMs(): number {
  return isObservationModel(selectedModelId) ? OBSERVATION_POLL_MS : LATEST_POLL_MS;
}

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

/** Horizontal grid samples the current view can actually display across
 * the whole world — its CSS pixel width at the current zoom times
 * devicePixelRatio (capped: beyond 2x extra grid columns are invisible). A
 * global 0.25 degree grid is 1440 columns over 360 degrees, so this is
 * directly comparable to a global variant's width; a regional grid's
 * columns cover only its own span, which `pickBundleVariant` scales the
 * need by (`bundleLongitudeSpan`). */
function neededGridWidth(): number {
  const worldCssWidth = 512 * 2 ** map.getZoom();
  return worldCssWidth * Math.min(2, window.devicePixelRatio || 1);
}

/** The degrees of longitude a bundle's grid covers, read off its poster's
 * metadata — the same grid at a coarser step, and in the manifest before
 * any bundle byte is — or the world's when the bundle ships no poster. */
function bundleLongitudeSpan(descriptor: ForecastManifest["bundles"][number]): number {
  if (!descriptor.poster) return 360;
  try {
    const grid = geoGrid(parseBundleMetadata(descriptor.poster.metadataJson));
    return Math.min(360, Math.abs(grid.width * grid.longitudeStep));
  } catch {
    return 360;
  }
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
 * ("GFS / TMP 2M", "ECMWF / TMP 2M"). A registered field's is read off the
 * variable table (`variableUi`); an unknown field's off its own metadata. */
interface VariableUi {
  code: string;
  title: readonly string[];
  bufferTitle: string;
  label: string;
  legend: readonly string[];
}

interface BasemapTones {
  ocean: string;
  land: string;
  /** Painted behind everything — visible past the coastlines' antialiasing
   * and across the whole canvas until the first tiles arrive. Defaults to
   * `ocean`, which is what a themed slate wants; a layer that leaves the
   * flavor alone names the flavor's own background instead. */
  background?: string;
}

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

/** The grounds, per theme (`VariableSpec.ground` says which a field takes).
 *
 * On the dark theme each is a slate the field's palette needs: an opaque
 * coat (the temperatures, θe) paints over its ground, so that one is nearly
 * a void; the translucent washes (precipitation, humidity, cloud, CAPE,
 * visibility, the ice and wave fields) composite over theirs, which has to
 * carry the geography and keep the low end of the ramp readable, so it is
 * lighter; the wind's is between; the radiation's and the radar's are their
 * own — radar echoes are small and bright, and the ground stays dark enough
 * for a 5 dBZ edge to read against it. The contour lines are drawn thin
 * over a nearly bare map, which is what a chart looks like: the base
 * carries the geography on its own, so it is the plainest of the set.
 *
 * Light means a light map: every field sits on the same white ground, the
 * translucent ones included, so the page is one sheet rather than paper
 * chrome floating over a dark map. Drizzle, the faintest wind and a 5 dBZ
 * edge read weaker on white than on the dark slate — the accepted cost of
 * one palette serving both themes (see PRECIPITATION_STOPS in palettes.ts).
 * The lines keep their chart stock: contours are a weather chart, and the
 * warm sheet is the design's own paper for one. */
const GROUND_TONES: Record<GroundId, { dark: BasemapTones; light: BasemapTones }> = {
  coat: { dark: { ocean: "#0b1826", land: "#182c3d" }, light: PAPER_GROUND },
  slate: { dark: { ocean: "#16344a", land: "#28495f" }, light: PAPER_GROUND },
  wind: { dark: { ocean: "#0e2131", land: "#1d3849" }, light: PAPER_GROUND },
  solar: { dark: { ocean: "#0d1b2b", land: "#1c3242" }, light: PAPER_GROUND },
  radar: { dark: { ocean: "#0c1a26", land: "#1a2f3d" }, light: PAPER_GROUND },
  chart: { dark: { ocean: "#101f2c", land: "#22384a" }, light: { ocean: "#dcd6c8", land: "#c9c2b2" } },
};

/** The ground under the field on screen. Read at every use rather than
 * once: the theme toggles in place. An unregistered field takes the coat's. */
function currentBasemapTheme(): BasemapTones {
  const id = document.body.dataset.variable;
  const ground = (id === undefined ? null : variableSpec(id)?.ground) ?? "coat";
  return isDark ? GROUND_TONES[ground].dark : GROUND_TONES[ground].light;
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
  tcLayers?.setInk(darkGround);
  stationLayers?.setInk(darkGround);
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
  // The coast reads darker than a border, the way it does on a chart.
  const coast = darkGround ? "rgba(255, 255, 255, 0.55)" : "rgba(27, 26, 23, 0.6)";
  for (const layer of map.getStyle().layers ?? []) {
    if (layer.type === "symbol") {
      map.setPaintProperty(layer.id, "text-color", ink);
      map.setPaintProperty(layer.id, "text-halo-color", halo);
    } else if (layer.id === "boundaries" || layer.id === "boundaries_country") {
      map.setPaintProperty(layer.id, "line-color", border);
    } else if (layer.id === COASTLINE_LAYER) {
      map.setPaintProperty(layer.id, "line-color", coast);
    }
  }
  applyLabelInk();
}

/** The coastline drawn as a line of its own. Protomaps has no coastline
 * layer: the coast is only where the `earth` fill meets the `water` fill,
 * and under a translucent field that edge all but disappears (Windy draws
 * its coast as a dark line over the data, which is what the eye expects).
 * A line layer over the `earth` polygons traces their outline — the ocean
 * coast, since lakes are `water` features inside the land — and the tile
 * clip edges fall in the tile buffer, where the renderer never paints. Its
 * width is a style property; its color follows the ground in
 * `applyBasemapInk`, like the boundaries'. */
const COASTLINE_LAYER = "coastline";
/** Coastline width in CSS px by zoom: a hairline would vanish under the
 * data at the global view, and past the regional scale the coast stays a
 * line rather than growing into an edge. */
const COASTLINE_WIDTH: NonNullable<NonNullable<LineLayer["paint"]>["line-width"]> = [
  "interpolate",
  ["linear"],
  ["zoom"],
  0,
  0.9,
  4,
  1.3,
  8,
  1.8,
];

/** Protomaps hosted basemap (real coastlines, waterways, boundaries and
 * labels). The forecast layers insert themselves before this layer — the
 * coastline, which sits just under the boundaries — keeping the coast, the
 * boundaries and the place labels legible above the data. */
const FORECAST_ANCHOR_LAYER = COASTLINE_LAYER;
const PROTOMAPS_KEY = "249bb192fefe0a77";

// maplibre-gl 6 no longer re-exports the style-spec types; take the style
// object's type from the map options that consume it.
type BasemapStyle = Exclude<MapOptions["style"], string | undefined>;
type LineLayer = Extract<BasemapStyle["layers"][number], { type: "line" }>;

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
    layers: withCoastline(
      quietedUnderData(
        basemapLayers("protomaps", flavor, { lang: basemapLang }).filter((layer) => layer.id !== "landcover"),
      ),
    ),
  };
}

/** The flavor's street-level detail, toned down so a field stays the
 * picture at a city zoom. The finest grids (the 0.005° JMA nowcast) earn
 * zoom 12, where a flavor draws every street, the building footprints and
 * the landuse fills; under a translucent field the street grid reads as
 * bright patches over the data and the bridges and major roads as white
 * bars across it. Buildings are dropped outright — a weather map has no
 * use for footprints — and the roads keep a fraction of their opacity,
 * the major ones more so the eye still has the arterials to hold on to.
 * Water, coast, boundaries and labels are untouched. */
function quietedUnderData(layers: BasemapStyle["layers"]): BasemapStyle["layers"] {
  return layers.flatMap((layer) => {
    if (layer.id === "buildings") return [];
    if (layer.type === "line" && layer.id.startsWith("roads_")) {
      const arterial = /highway|major/.test(layer.id);
      return [{ ...layer, paint: { ...layer.paint, "line-opacity": arterial ? 0.55 : 0.3 } }];
    }
    if (layer.type === "fill" && layer.id.startsWith("landuse_")) {
      return [{ ...layer, paint: { ...layer.paint, "fill-opacity": 0.5 } }];
    }
    return [layer];
  });
}

/** The flavor's layers with the coastline inserted just under the country
 * boundaries, so the two read as one set of lines over the data. */
function withCoastline(layers: BasemapStyle["layers"]): BasemapStyle["layers"] {
  const coastline: BasemapStyle["layers"][number] = {
    id: COASTLINE_LAYER,
    type: "line",
    source: "protomaps",
    "source-layer": "earth",
    filter: ["==", "$type", "Polygon"],
    layout: { "line-join": "round", "line-cap": "round" },
    // The color is the ground's ink, set by `applyBasemapInk`; this is only
    // what the layer carries until the style has loaded.
    paint: { "line-color": "rgba(27, 26, 23, 0.6)", "line-width": COASTLINE_WIDTH },
  };
  const at = layers.findIndex((layer) => layer.id === "boundaries_country");
  if (at < 0) throw new Error("basemap style has no boundaries_country layer");
  return [...layers.slice(0, at), coastline, ...layers.slice(at)];
}

setWorkerUrl(maplibreWorkerUrl);

/** The basemap style the map currently carries, as built — what
 * `syncBasemapStyle` diffs the next build against. */
let appliedBasemapStyle: BasemapStyle = buildBasemapStyle();

/** The camera the link fixed, if it did (`#map=<zoom>/<lat>/<lon>`). The
 * map reads and writes the fragment itself (`hash` below): this is read
 * once, before anything moves the camera, and says whether the dataset's
 * own framing yields to the sharer's view. */
const urlCamera = parseCameraFromHash(window.location.hash);

/** The zoom ceiling with nothing finer than the global models on screen.
 * A finer grid raises it (`applyZoomCeiling`): the ceiling follows the
 * data, since past the point where a cell is `ZOOM_CEILING_CELL_PIXELS`
 * wide the map only magnifies the interpolation. */
const BASE_MAX_ZOOM = 7;
/** The ceiling while station marks are drawn. Airports sit a few
 * kilometres apart — a city's civil and military fields, a pair of
 * regional strips — and at the models' ceiling those are one dot; the
 * marks are worth three more levels even where the field under them is
 * only being magnified. */
const STATION_MAX_ZOOM = 10;
/** What the data on screen is worth, kept so the station switch can be
 * folded in without reading the grid again. */
let dataZoomCeiling = BASE_MAX_ZOOM;
/** How wide a grid cell may get on screen before the zoom stops — 32 CSS
 * px puts the 0.005° JMA nowcast at zoom 12, a 0.02° radar mosaic at 10
 * and the 0.03° HRRR grid at 9.5, while the 0.25° models stay at the base
 * ceiling. */
const ZOOM_CEILING_CELL_PIXELS = 32;
/** How far a link's camera is honoured before any dataset has said what
 * its grid earns: the ceiling of the finest grid published. A link into a
 * regional dataset at a city zoom opens where it points instead of at the
 * base ceiling; the dataset's own ceiling (`applyZoomCeiling`) settles it
 * once the manifest is in. */
const DEEP_LINK_MAX_ZOOM = 12.5;

const map = new MaplibreMap({
  container: "map",
  center: [128, 28],
  zoom: 1.65,
  minZoom: 0,
  maxZoom: urlCamera ? Math.max(BASE_MAX_ZOOM, Math.min(urlCamera.zoom, DEEP_LINK_MAX_ZOOM)) : BASE_MAX_ZOOM,
  // The view lives in the fragment, `#map=<zoom>/<lat>/<lon>`, kept
  // current on every move — so a copied address reproduces the view, and
  // the query string, which is what names the page, never changes on a pan.
  hash: "map",
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
 * it. Both flavors and all eleven label languages come out of the same
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
const validTime = required<HTMLTimeElement>("valid-time");
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
const legendDerived = required<HTMLElement>("legend-derived");
const legendInflow = required<HTMLElement>("legend-inflow");
const legendFront = required<HTMLElement>("legend-front");
const trackStart = required<HTMLElement>("track-start");
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
const tcTile = required<HTMLButtonElement>("tc-tile");
const soundingTile = required<HTMLButtonElement>("sounding-tile");
const airportTile = required<HTMLButtonElement>("airport-tile");
const tcSheet = required<HTMLElement>("tc-sheet");
const tcList = required<HTMLElement>("tc-list");
const fieldMore = required<HTMLButtonElement>("field-more");
const fieldSheet = required<HTMLElement>("field-sheet");
const fieldList = required<HTMLElement>("field-list");
// Scoped to buttons: <body> carries data-variable/data-model too (styling
// state), and must never be hidden or aria-pressed like a switch button.
const variableRail = document.querySelector<HTMLElement>(".variable-rail");
/** The rail's field section: the core tiles, the tile of the field on
 * screen, MORE. */
const fieldSection = variableRail?.querySelector<HTMLElement>('.rail-section[data-section="field"]') ?? null;
/** The layer rail's tiles that name a bundle: the field section's, and the
 * pressure switch in the overlay section. Read live rather than
 * snapshotted: the shell writes one tile per field it knows, and a field on
 * screen this build has never heard of gets a generic tile written for it
 * (`syncFieldTiles`) — a layer nobody can see is the same as one nobody
 * can reach. */
function variableButtons(): HTMLButtonElement[] {
  return variableRail ? [...variableRail.querySelectorAll<HTMLButtonElement>("button[data-variable]")] : [];
}
/** The field section's written tiles — every field this build has an icon
 * for, whether or not the run ships it or the rail shows it. */
function fieldTiles(): HTMLButtonElement[] {
  return fieldSection ? [...fieldSection.querySelectorAll<HTMLButtonElement>("button[data-variable]:not([data-unknown])")] : [];
}
/** The experiment's toggle tiles for its derived layers. */
function derivedButtons(): HTMLButtonElement[] {
  return variableRail ? [...variableRail.querySelectorAll<HTMLButtonElement>("button[data-derived]")] : [];
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
  aifs: "ECMWF / AIFS SINGLE (0.25°)",
  sflux: "NOAA / GFS SFLUX (13 KM)",
  hrrr: "NOAA / HRRR CONUS (3 KM)",
  cma: "CMA / RADAR MOSAIC (0.044°)",
  mrms: "NOAA / MRMS CONUS (0.02°)",
  jma: "JMA / NOWCAST JAPAN (0.005°)",
  himawari: "JMA / HIMAWARI-9 AHI (0.04°)",
};

/** The member to open when a family's one rail tile is picked: whichever
 * level was last on screen, else the first the run publishes — the surface
 * member where the family has one (2 m temperature, 10 m wind, sea level
 * pressure), the lowest isobaric surface otherwise. */
const lastFamilyMember = new Map<IsobaricFamily, ForecastBundleId>();
/** The field last on screen, for the way back from the lines-alone view:
 * the level row's ALONE member and the rail's pressure switch both put it
 * back. Null until a field has been drawn, in which case the dataset's own
 * default stands in. */
let lastField: ForecastBundleId | null = null;

function preferredFamilyMember(family: IsobaricFamily): ForecastBundleId {
  const run = manifest;
  const available = run ? familyMembers(family, (id) => hasBundle(run, id)).filter((id) => hasBundle(run, id)) : familyMembers(family);
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
const tcSheetControl = createSheet({
  trigger: tcTile,
  sheet: tcSheet,
  canOpen: () => tcLoaded !== null && activeCase === null,
});

const langSheetControl = createSheet({
  trigger: langTrigger,
  sheet: langSheet,
  initialFocus: (sheet) => sheet.querySelector<HTMLButtonElement>("button[aria-current]"),
});

/** The field sheet: every field the run publishes, hung from the rail's
 * MORE tile; its rows are written by `renderFieldSheet` whenever the run
 * or the field on screen changes. */
const fieldSheetControl = createSheet({
  trigger: fieldMore,
  sheet: fieldSheet,
  canOpen: () => manifest !== null && !switchingVariable,
  initialFocus: (sheet) => sheet.querySelector<HTMLButtonElement>('button[aria-pressed="true"]'),
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
   * half-resolution variant tier; "Zarr" and "Zarr ½" the same tiers read
   * through the Zarr channel). */
  format: "H.264" | "Xue" | "Xue ½" | "Zarr" | "Zarr ½";
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
/** The crc of the live manifest on screen, off the pointer that named it —
 * what the pointer poll compares against; null for a case. */
let currentManifestCrc: string | null = null;
let metadata: BundleMetadata | null = null;

// What is on screen is `view` (viewstate.ts): the composition — a filled
// field and the contour lines over it, each slot its own session (own
// worker, own grid, own tiles, own resolution tier), the **primary** (the
// fill's, or the lines' when there is no fill) driving the timeline, the
// legend, the data card and the ground tone, the lines following it by
// lead time — plus the overlays and the marks.

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
  for (const session of compositeSessions()) list.push(session);
  return list;
}

/** Wind GPU particle layer; created alongside the scalar layer and toggled by
 * the active variable. */
let windLayer: WindParticleLayer | null = null;
let windLayerAdded = false;
let windLayerGridSource: BundleMetadata | null = null;

/** One field the experiment computes: its layer, the grid it was last
 * configured for, and the input planes the plane on screen was built from
 * (their joined cache keys), so a repeat select never recomputes. */
interface DerivedLayer {
  id: "inflow" | "front";
  layer: ForecastLayer;
  gridSource: BundleMetadata | null;
  builtFrom: string | null;
}

/** The experiment's state: the two input sessions, opened beside the
 * composition's own like overlays, the derived layers, and the cache keys
 * they are waiting on or showing. Null unless `?x=true`. */
interface Composite {
  flow: VariableSession | null;
  warmth: VariableSession | null;
  layers: Record<DerivedLayer["id"], DerivedLayer>;
  /** Input planes not decoded yet for the frame on screen: a decode landing
   * on one of these retries the composite. */
  wantedKeys: Set<string>;
  /** Input planes the derived fields on screen were built from — protected
   * from eviction like a displayed plane. */
  shownKeys: string[];
}

function makeDerivedLayer(id: DerivedLayer["id"]): DerivedLayer {
  return {
    id,
    layer: new ForecastLayer((message) => showError(message), `forecast-derived-${id}`),
    gridSource: null,
    builtFrom: null,
  };
}

const composite: Composite | null = experimentEnabled
  ? {
      flow: null,
      warmth: null,
      layers: { inflow: makeDerivedLayer("inflow"), front: makeDerivedLayer("front") },
      wantedKeys: new Set(),
      shownKeys: [],
    }
  : null;

/** Whether a session feeds the experiment's derived fields. Such a session
 * always decodes whole planes: the fields are computed over the grid, and
 * the particles the flow runs respawn anywhere on it. */
function isCompositeInput(session: VariableSession): boolean {
  return composite !== null && (session === composite.flow || session === composite.warmth);
}

/** The experiment's input sessions that are not otherwise on screen. */
function compositeSessions(): VariableSession[] {
  if (!composite) return [];
  const shown = new Set<VariableSession>();
  if (activeSession) shown.add(activeSession);
  for (const slot of overlaySlots()) shown.add(slot.session!);
  return [composite.flow, composite.warmth].filter(
    (session): session is VariableSession => session !== null && !shown.has(session),
  );
}
/** Shareable URL entry (e.g. /?model=ecmwf&type=wind) picks the initial model
 * and layer; a missing or unrecognized param falls back to the default. */
let selectedModelId: ForecastModelId = parseModelFromSearch(window.location.search);
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
/** Which channel a session reads its bundle through. `zarr` — the default
 * — is the Zarr store a run publishes, for the bundles that have one, and
 * the container for the rest; `?backend=xue` takes the container wherever
 * one is published. A comparison path: the same decoder over another
 * index. */
const dataBackend = parseBackendFromSearch(window.location.search);
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
const requestedView = parseView(window.location.search, {
  field: experimentEnabled ? EXPERIMENT_FILL : DEFAULT_VARIABLE,
  lines: experimentEnabled ? EXPERIMENT_LINES : null,
  particles: storedParticles() ?? !reducedMotion.matches,
});
/** What is on screen, or being switched to: the field and the lines (the
 * composition), the overlays, the marks. One object, mutated in place;
 * the rail's pressed states (`syncRail`), the level row and the address
 * bar (`syncUrl`) are projections of it. */
const view: ViewState = requestedView.view;
/** Whether the layer on screen was asked for — by `?type=` or by a press
 * on the rail — rather than defaulted. A defaulted layer follows the
 * dataset: opening or switching to the radar mosaic shows its
 * reflectivity, a forecast its precipitation; a chosen layer is kept
 * across a model switch as it always was. */
let variableChosen = requestedView.fieldRequested;
/** Whether the overlay's state is a choice — the URL's or the viewer's —
 * rather than this device's default. Only a choice is written back into the
 * address bar: a reduced-motion visitor who never touched the switch would
 * otherwise hand out links that turn the overlay off for everyone. */
let particlesChosen = requestedView.particlesRequested;
/** The primary bundle: the fill's, or the lines' when nothing is filled. */
let selectedVariableId: ForecastBundleId = compositionPrimary(viewComposition(), DEFAULT_VARIABLE);

/** The view's two slots, as the overlay and session code reads them. */
function viewComposition(): ViewComposition {
  return { fill: view.field, lines: view.lines };
}

function setComposition(next: ViewComposition): void {
  view.field = next.fill;
  view.lines = next.lines;
}

/** The tone the particles are drawn in over the speed field: a bright trace
 * on the dark theme, the paper theme's own ink on white. Partly transparent
 * either way — the field underneath has to read through the trails, and the
 * particles are there for direction and pace, not for a value. */
function particleInk(): readonly [number, number, number, number] {
  return isDark ? [1, 1, 1, 0.45] : [0.11, 0.1, 0.09, 0.4];
}

/** The ink of the experiment's flow particles over a scalar fill: the
 * inflow tint's own red-brown, heavier than the wind's grey so the stream
 * reads over the stepped rain and the chart paper. */
function experimentFlowInk(): readonly [number, number, number, number] {
  return isDark ? [1, 0.72, 0.6, 0.7] : [0.55, 0.16, 0.08, 0.85];
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

/** A valid time, in the display zone — the browser's, or the pinned
 * point's (`timezone.ts`). */
function formatDate(value: string | number): string {
  return formatStamp(value, displayZone);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCompactDate(value: number): string {
  return formatCompactStamp(value, displayZone);
}

/** Short weekday in the UI locale and the day of month, read in the display
 * zone like every other valid-time stamp. */
function formatDayMark(value: number): string {
  return formatDayMarkIn(value, displayZone, htmlLang);
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
 * A radar mosaic has no run cycle and no lead time: its runTime is only
 * where the window starts, each frame is an observation, and the timeline
 * reads as the observation's clock time rather than a forecast hour. */
function showingObservations(): boolean {
  return isObservationModel(activeCase ? activeCase.modelId : selectedModelId);
}

/** Retitle the timeline and station panel for the kind of dataset on screen.
 * The instrument-panel control label stays English in both locales, like
 * every other one. */
function applyDatasetWording(): void {
  const observations = showingObservations();
  leadLabel.textContent = observations ? "OBSERVED" : "FORECAST HOUR";
  runTimeLabel.textContent = t(observations ? "latestObservation" : "runCycle");
  validTimeLabel.textContent = t(observations ? "observationTimeLabel" : "validTimeLabel");
  slider.setAttribute("aria-label", t(observations ? "observationTimeLabel" : "forecastHourAria"));
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

/** The frame whose valid time is nearest to `validTime` (a millisecond
 * epoch), on the active axis — how a playhead keeps its place across runs
 * whose run times differ. */
function nearestFrameIndexForValidTime(validTime: number): number {
  const base = metadata ? Date.parse(metadata.runTime) : 0;
  return nearestFrameIndex((validTime - base) / 1000);
}

/** The instrument readout. A forecast reads as the forecast hour it has
 * always been ("F058"; a sub-hourly axis carries the minutes too,
 * "F058:06"). An observation has no run to count from, so it reads as the
 * frame's clock time in the display zone ("09:50"). */
function formatLead(index: number): string {
  if (showingObservations()) return formatClockStamp(frameValidTime(index), displayZone);
  const seconds = frameLeadSeconds(index);
  const hours = Math.floor(seconds / HOUR_SECONDS);
  const label = `F${String(hours).padStart(3, "0")}`;
  const minutes = Math.floor((seconds % HOUR_SECONDS) / 60);
  return minutes === 0 && frameUnitSeconds >= HOUR_SECONDS
    ? label
    : `${label}:${String(minutes).padStart(2, "0")}`;
}

/** The readout with the frame's date beside it, for the tooltip and the
 * probe: a forecast's hour and valid time, an observation's stamp alone
 * (its readout is already that stamp's clock). */
function frameStampLine(index: number): string {
  const stamp = formatCompactDate(frameValidTime(index));
  return showingObservations() ? stamp : `${formatLead(index)} · ${stamp}`;
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
    `clock: ${displayZone}${probeZone ? " (pinned)" : ""}`,
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
//
// Under the variable on screen sit the meteogram rows (meteogram.ts): the
// surface quantities a run publishes, each read at the same cell from its
// own bundle. Those bundles are opened as *probe sessions* — the same
// streaming session a layer would get, minus any plane ever decoded: the
// structural prefix and then one chunk per temporal group of the one tile,
// which is what makes five more bundles affordable behind one click.
let probe: ProbeSeries | null = null;
/** The pin on the map: the panel sits over the capsule, so the point it
 * reads is marked where it was clicked. */
let probeMarker: Marker | null = null;
let probeRenderFrame: number | null = null;
/** The pinned point's zone once the lookup has answered, and the pin it
 * answered for: an answer for a pin since replaced must not land on the
 * new one, and the first pin's answer can arrive seconds later, behind the
 * index download. */
let probeZone: string | null = null;
let probePinSequence = 0;
/** Series requests in flight, so a re-render or a session swap does not ask
 * twice for the same cell. Keyed by `variableId:column:row`. */
const probeSeriesRequests = new Set<string>();

// The two point products answer a pinned point too, and not as marks: the
// nearest radiosonde ascent becomes a skew-T under the rows, and the
// nearest airport's last day of reports is laid over the rows themselves.
// Both are resolved from whatever indexes have loaded — a pin reads them
// whether or not the rail tile is pressed — and both are bounded by a
// radius (`stations/nearest.ts`), because a station far enough away is
// somebody else's weather rather than a faint reading of this one.
//
// The sounding station a pin resolved to is state of the section that
// draws it (`soundingSection`), which owns the ascent it fetched and the
// nominal time selected on it; `closeProbe` hands it none. The airport has
// no such owner, so its state is here.
/** The airport this pin resolved to, its distance, and its history once the
 * one range request has answered. */
let probeAirport: {
  station: AirportStation;
  distanceKm: number;
  history: AirportStationHistory | null;
} | null = null;
/** The pin the airport history in flight belongs to; an answer for a pin
 * since replaced must not land on the new one. */
let probeAirportSequence = 0;

/** The day strip and the row pitch of the meteogram, shared by the canvas
 * and the DOM rows beside it so the two stay aligned. A row is two lines of
 * text — the code, then the readout with its unit — and the pitch is what
 * those need. */
const METEOGRAM_HEADER_HEIGHT = 12;
const METEOGRAM_ROW_HEIGHT = 30;

/** The skew-T under the rows. Built before the panel, since the panel puts
 * its root in place; it reads the model column back out of main.ts at
 * draw time rather than being handed one. */
const soundingSection: SoundingSection = createSoundingSection({
  modelProfile: () => modelProfileForSounding(),
  formatTime: (time) => formatCompactDate(Date.parse(time)),
  // Read at pin time, never captured: `view.marks.stations` follows the rail.
  wantsOpen: () => view.marks.stations.soundings,
  modelPossible: () =>
    manifest !== null &&
    !showingObservations() &&
    modelProfileBundles(manifest.bundles.map((entry) => entry.variable)).length > 0,
  onChange: () => {
    // A newly opened section, or another ascent selected, wants the run's
    // isobaric levels at the pinned cell.
    ensureProbeSessions();
    paintSkewtSheet();
  },
});

const probePanel = buildProbePanel();

// The full-height copy of the chart, one of the shell's sheets: the panel's
// 320 px is a reading, and a sheet is what a forecaster actually works the
// profile in. The section owns the trigger, so the press, the focus and the
// Escape are `sheet.ts`'s as they are for the model and sources sheets.
const skewtSheet = document.getElementById("skewt-sheet");
const skewtSheetCanvas = document.getElementById("skewt-sheet-chart") as HTMLCanvasElement | null;
const skewtSheetTitle = document.getElementById("skewt-sheet-title");
const skewtSheetController = skewtSheet
  ? createSheet({
      trigger: soundingSection.expandButton,
      sheet: skewtSheet,
      canOpen: () => soundingSection.isOpen(),
    })
  : null;

/** Paint the sheet's canvas, if it is open, at whatever room the viewport
 * gives it. */
function paintSkewtSheet(): void {
  if (!skewtSheetController?.isOpen() || !skewtSheetCanvas) return;
  if (skewtSheetTitle) skewtSheetTitle.textContent = soundingSection.headline();
  const width = skewtSheetCanvas.clientWidth;
  const height = Math.max(320, Math.min(window.innerHeight - 160, Math.round(width * 1.25)));
  soundingSection.drawInto(skewtSheetCanvas, width, height);
}

soundingSection.expandButton.addEventListener("click", () => {
  // After `sheet.ts`'s own listener, so the sheet is already open here.
  window.requestAnimationFrame(paintSkewtSheet);
});
window.addEventListener("resize", () => paintSkewtSheet());

/** Whether the Escape now being handled found the chart sheet open. The
 * sheet closes itself on Escape (`sheet.ts`), and the shell's own handler —
 * which unpins the probe — runs after it and would otherwise see a closed
 * sheet and take the panel down with it. One Escape closes one thing. */
let skewtSheetTookEscape = false;
window.addEventListener(
  "keydown",
  (event) => {
    if (event.key === "Escape") skewtSheetTookEscape = skewtSheetController?.isOpen() ?? false;
  },
  true,
);

/** The panel docks over the transport capsule at the capsule's own width,
 * so the rows get the track's length. Built once, hidden until a point is
 * pinned. */
function buildProbePanel() {
  const root = document.createElement("section");
  root.className = "probe-panel";
  root.id = "probe-panel";
  root.setAttribute("aria-label", t("probeAria"));
  root.hidden = true;
  // The headline is the first row of the same two columns the meteogram
  // rows and the capsule use: what is read and its value at the playhead
  // in the label column, and on the axis column the frame, the cell and how
  // much of the series is in hand, with the sparkline under them.
  const head = document.createElement("div");
  head.className = "probe-head";
  const headline = document.createElement("div");
  headline.className = "probe-headline";
  const axis = document.createElement("div");
  axis.className = "probe-axis";
  const metaLine = document.createElement("div");
  metaLine.className = "probe-meta-line";
  const code = document.createElement("span");
  code.className = "probe-code";
  code.id = "probe-code";
  // The nearest airport, beside the code the panel is reading: its ICAO id
  // and the flight category chip the station card uses, in the category's
  // own color. Empty — and out of the layout — when no airport is in range.
  const airport = document.createElement("span");
  airport.className = "probe-airport";
  airport.id = "probe-airport";
  airport.hidden = true;
  const airportId = document.createElement("b");
  airportId.className = "probe-airport-id";
  const airportCategory = document.createElement("span");
  airportCategory.className = "probe-airport-category";
  airport.append(airportId, airportCategory);
  const value = document.createElement("output");
  value.className = "probe-value";
  value.id = "probe-value";
  const meta = document.createElement("span");
  meta.className = "probe-meta";
  meta.id = "probe-meta";
  const coords = document.createElement("span");
  coords.className = "probe-coords";
  coords.id = "probe-coords";
  // The zone the pinned point lies in, which every valid time on screen now
  // reads in; empty until the lookup answers.
  const zone = document.createElement("span");
  zone.className = "probe-zone";
  zone.id = "probe-zone";
  const footer = document.createElement("span");
  footer.className = "probe-footer";
  const count = document.createElement("span");
  count.className = "probe-count";
  count.id = "probe-count";
  const hint = document.createElement("span");
  hint.id = "probe-hint";
  footer.append(count, hint);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "probe-close";
  close.setAttribute("aria-label", t("probeCloseAria"));
  close.innerHTML =
    '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" /></svg>';
  close.addEventListener("click", closeProbe);
  const canvas = document.createElement("canvas");
  canvas.className = "probe-chart";
  canvas.setAttribute("aria-hidden", "true");
  const codeLine = document.createElement("span");
  codeLine.className = "probe-code-line";
  codeLine.append(code, airport);
  headline.append(codeLine, value);
  metaLine.append(meta, coords, zone, footer, close);
  axis.append(metaLine, canvas);
  head.append(headline, axis);
  // The meteogram: the row labels and readouts are DOM text in the left
  // column, the traces one canvas beside them, both on the same row pitch.
  const rows = document.createElement("div");
  rows.className = "probe-rows";
  rows.id = "probe-rows";
  rows.hidden = true;
  const rowList = document.createElement("div");
  rowList.className = "probe-row-list";
  rowList.style.paddingTop = `${METEOGRAM_HEADER_HEIGHT}px`;
  const rowsChart = document.createElement("canvas");
  rowsChart.className = "probe-rows-chart";
  rowsChart.setAttribute("aria-hidden", "true");
  rows.append(rowList, rowsChart);
  // The sounding section is the last thing in the panel: the rows are the
  // point's own forecast, and the ascent beside it is context under them.
  root.append(head, rows, soundingSection.root);
  timelinePanel.parentElement!.insertBefore(root, timelinePanel);
  return {
    root,
    code,
    airport,
    airportId,
    airportCategory,
    coords,
    zone,
    value,
    meta,
    canvas,
    count,
    hint,
    close,
    rows,
    rowList,
    rowsChart,
  };
}

/** One meteogram row's DOM: its code, its readout at the playhead, and the
 * state the tests and the stylesheet read. Rebuilt when the run's row set
 * changes, updated in place otherwise. */
interface ProbeRowElements {
  spec: MeteogramRowSpec;
  root: HTMLElement;
  code: HTMLElement;
  value: HTMLOutputElement;
  /** The line under the value: the unit, and the wind row's direction. */
  note: HTMLElement;
}

let probeRowElements: ProbeRowElements[] = [];

/** The rows this run can fill, or none before a manifest is up. */
function currentMeteogramRows(): MeteogramRowSpec[] {
  const run = manifest;
  return run ? meteogramRows((id) => hasBundle(run, id)) : [];
}

function syncProbeRowElements(specs: MeteogramRowSpec[]): void {
  const same =
    probeRowElements.length === specs.length &&
    probeRowElements.every((row, index) => row.spec.bundles.join() === specs[index]!.bundles.join());
  if (same) return;
  probeRowElements = specs.map((spec) => {
    const root = document.createElement("div");
    root.className = "probe-row";
    root.dataset.row = spec.id;
    root.style.height = `${METEOGRAM_ROW_HEIGHT}px`;
    const code = document.createElement("span");
    code.className = "probe-row-code";
    code.textContent = meteogramRowCode(spec);
    const value = document.createElement("output");
    value.className = "probe-row-value";
    const note = document.createElement("span");
    note.className = "probe-row-note";
    // One line for the readout and its unit: the row is a flex column, and
    // would put two children on two lines.
    const line = document.createElement("span");
    line.className = "probe-row-line";
    line.append(value, note);
    root.append(code, line);
    return { spec, root, code, value, note };
  });
  probePanel.rowList.replaceChildren(...probeRowElements.map((row) => row.root));
  probePanel.rows.hidden = specs.length === 0;
}

/** The pin's element: a ring in the theme's ink, drawn by the stylesheet. */
function buildProbePin(): HTMLElement {
  const pin = document.createElement("div");
  pin.className = "probe-pin";
  pin.setAttribute("aria-hidden", "true");
  return pin;
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
  // Off a regional model's own footprint the grid still holds a value — the
  // encoder's edge extension — but no forecast; there is nothing to pin.
  const domain = modelDomain();
  if (domain && !domainContains(domain, lambertCone(domain), longitude, latitude)) {
    closeProbe();
    return;
  }
  probe = new ProbeSeries(longitude, latitude);
  // The requests tracked for the previous pin say nothing about this cell,
  // and keeping them would suppress the series read when a point is pinned
  // again later.
  probeSeriesRequests.clear();
  resolveProbeStations(longitude, latitude);
  void resolveProbeZone(longitude, latitude);
  seedProbeFromCache();
  requestAllProbeSeries();
  ensureProbeSessions();
  if (!probeMarker) probeMarker = new Marker({ element: buildProbePin(), anchor: "center" });
  probeMarker.setLngLat([longitude, latitude]);
  probeMarker.addTo(map);
  probePanel.root.hidden = false;
  renderProbe();
}

/** The two point products at the pinned point: the nearest ascent for the
 * chart, the nearest airport for the rows. Both are resolved from the
 * indexes already loaded — a product not published, or not polled yet, is
 * simply absent — and neither depends on its rail tile being pressed.
 *
 * The airport's day of reports is one range request, made here rather than
 * at draw time so the rows fill in once rather than per frame. */
function resolveProbeStations(longitude: number, latitude: number): void {
  const sounding = soundingLoaded ? nearestSounding(soundingLoaded.index, longitude, latitude) : null;
  soundingSection.setStation(soundingLoaded, sounding?.station ?? null, sounding?.distanceKm ?? 0);

  const airport = airportLoaded ? nearestAirport(airportLoaded.index, longitude, latitude) : null;
  probeAirport = airport
    ? { station: airport.station, distanceKm: airport.distanceKm, history: null }
    : null;
  const sequence = ++probeAirportSequence;
  if (!airport || !airportLoaded) return;
  const source = airportLoaded;
  void fetchAirportStation(source, airport.station)
    .then((history) => {
      if (sequence !== probeAirportSequence || !probeAirport) return;
      probeAirport.history = history;
      scheduleProbeRender();
    })
    .catch((error: unknown) => {
      // Diagnostics stay English: the rows simply carry no observations.
      console.warn(
        `airport: ${airport.station.icao} history not read:`,
        error instanceof Error ? error.message : error,
      );
    });
}

function closeProbe(): void {
  if (!probe) return;
  probe = null;
  probeSeriesRequests.clear();
  probeAirport = null;
  probeAirportSequence += 1;
  soundingSection.setStation(null, null, 0);
  probeMarker?.remove();
  probePanel.root.hidden = true;
  // Unpinned, the clock is the viewer's own again.
  probePinSequence += 1;
  probeZone = null;
  setDisplayZone(null);
}

/** Read the pinned point's zone and move every valid time onto it. Until
 * the answer comes (the first time, after a 4 MB index has loaded) the
 * stamps keep the zone they had, so a pin never blanks the capsule. */
async function resolveProbeZone(longitude: number, latitude: number): Promise<void> {
  const sequence = ++probePinSequence;
  // The previous pin's zone says nothing about this point.
  probeZone = null;
  const zone = await zoneAt(longitude, latitude);
  if (sequence !== probePinSequence) return;
  probeZone = zone;
  setDisplayZone(zone);
  renderProbe();
}

/** Every session the pinned point reads: the ones on screen and the
 * meteogram's, resident or still opening. */
function probeSessions(): VariableSession[] {
  const list = slotSessions();
  for (const id of probeBundleIds()) {
    const session = sessions.get(id);
    if (session && !list.includes(session)) list.push(session);
  }
  return list;
}

/**
 * The bundles a pinned point reads: the meteogram's rows always, and the
 * run's isobaric levels while the sounding section is open — the model
 * column laid over the ascent is the same kind of read as a row, one probe
 * session per bundle, a few dozen kilobytes each.
 *
 * Nothing is opened for a section that is closed or has no station: a pin
 * with no sonde within 150 km costs exactly what it did before.
 */
function probeBundleIds(): string[] {
  const ids: string[] = [];
  for (const spec of currentMeteogramRows()) ids.push(...spec.bundles);
  if (manifest && soundingSection.isOpen()) {
    for (const bundle of modelProfileBundles(manifest.bundles.map((entry) => entry.variable))) {
      ids.push(bundle.id);
    }
  }
  return ids;
}

/** Open the meteogram's bundles for the pinned point, quietly and without
 * ever downloading one whole: a bundle the origin cannot range-serve is
 * left closed, and its row simply stays empty. A bundle already open for
 * the screen is reused as it is. Each session asks for its series the
 * moment it is ready. */
function ensureProbeSessions(): void {
  if (!probe || !manifest || !ready) return;
  const sequence = initializeSequence;
  for (const id of probeBundleIds()) {
    const resident = sessions.get(id);
    if (resident) {
      requestProbeSeries(resident);
      continue;
    }
    void loadVariable(id, sequence, "probe")
      .then((session) => {
        if (sequence !== initializeSequence || !probe) return;
        requestProbeSeries(session);
        scheduleProbeRender();
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // Diagnostics stay English: a row that cannot open is a console
        // note, never an error panel over the map.
        console.warn(`meteogram: ${id} not opened:`, error instanceof Error ? error.message : error);
      });
  }
}

/** Ask every probed session for the pinned cell's whole series. */
function requestAllProbeSeries(): void {
  for (const session of probeSessions()) requestProbeSeries(session);
}

/** Ask one session's decoder for the pinned cell's whole series, one
 * request per data variable. Only a tiled bundle can answer; everywhere else
 * the opportunistic sampling in `handleDecodedFrame` remains the only source.
 */
function requestProbeSeries(session: VariableSession): void {
  if (!probe || !session.tiles || !ready) return;
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
    syncProbeRowElements([]);
    renderProbeAirportChip();
    soundingSection.render();
    return;
  }
  const variable = session.variable;
  const cell = series.cellFor(session.metadata);
  probePanel.code.textContent = variableUi(session).code;
  renderProbeAirportChip();
  const point = cell ?? { longitude: series.longitude, latitude: series.latitude };
  probePanel.coords.textContent =
    `${formatProbeDegrees(point.latitude, "NS")} ${formatProbeDegrees(point.longitude, "EW")}`;
  probePanel.zone.textContent = probeZone ? zoneDisplayName(probeZone, frameValidTime(activeFrameIndex ?? Number(slider.value))) : "";

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
      typeof current === "number"
        ? `${formatProbeValue(variable, displayValue(variable.unit, current))} ${displayUnit(variable.unit)}`
        : "--";
    const lead = frameStampLine(index);
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
  renderProbeRows(series, index);
  // The ascent under the rows: its own time, never the playhead's, but the
  // model column laid over it comes from the series just read.
  soundingSection.render();
  paintSkewtSheet();
}

/** The nearest airport beside the panel's code: its ICAO id and the flight
 * category in the category's own color, the chip the station card uses. An
 * id and a category are instrument text and are never translated. */
function renderProbeAirportChip(): void {
  const airport = probeAirport;
  probePanel.airport.hidden = airport === null;
  if (!airport) return;
  probePanel.airportId.textContent = airport.station.icao;
  const category = airport.history?.metars[0]?.category ?? airport.station.category;
  probePanel.airportCategory.textContent = category ?? "";
  probePanel.airportCategory.hidden = category === null;
  probePanel.airport.style.setProperty(
    "--card-ink",
    categoryColor(category, document.body.dataset.ground === "dark"),
  );
}

/** The axis observations and forecast periods are placed against: the run
 * on screen, and the lead seconds of its first and last frame. Null before
 * a run is up. */
function probeObservationAxis(leads: readonly number[]): ObservationAxis | null {
  if (!metadata || leads.length === 0) return null;
  return {
    runTimeMs: Date.parse(metadata.runTime),
    firstLead: leads[0]!,
    lastLead: leads[leads.length - 1]!,
  };
}

/** One bundle's series at the pinned point on the *primary* axis: a lead
 * time the bundle's own axis lacks is a gap, and a session not open yet
 * (or a point off its grid) is a series of nothing. */
function probeSessionSeries(
  series: ProbeSeries,
  session: VariableSession | undefined,
  leads: readonly number[],
): { values: ProbeValue[]; state: ReturnType<typeof seriesState> } {
  if (!session || !series.cellFor(session.metadata)) {
    return { values: leads.map(() => undefined), state: "empty" };
  }
  const variables = probeVariables(session);
  const offsetFor = (lead: number) => sessionOffsetForLead(session, lead);
  const values = alignSeries(leads, offsetFor, (offset) => probeSeriesValues(series, variables, [offset])[0]);
  return { values, state: seriesState(leads, offsetFor, values) };
}

/**
 * The model column at the selected ascent's time, for the skew-T.
 *
 * The frame is chosen by valid time, not by the playhead: the chart is
 * showing an ascent released at a synoptic hour, and what belongs over it
 * is the forecast *for that hour*, whichever frame that is. Past three
 * hours (`MODEL_PROFILE_TOLERANCE_MS`) there is no frame near enough and
 * the chart draws the sonde alone, with the legend saying so.
 *
 * Every level comes out of the probe series the sessions have already read
 * at the pinned cell — the same one round trip per bundle the rows use — so
 * the column costs nothing beyond those sessions. A wind bundle is read as
 * its two components rather than as the magnitude the rows draw: a profile
 * wants the barb, which needs the direction.
 */
function modelProfileForSounding(): { profile: Profile; run: string; validTime: number } | null {
  const series = probe;
  if (!series || !manifest || !metadata) return null;
  const time = soundingSection.selectedTime();
  if (time === null) return null;
  const offsets = frameAxis();
  const index = nearestFrameForTime(
    offsets.map((_, position) => frameValidTime(position)),
    Date.parse(time),
  );
  if (index === null) return null;
  const lead = frameLeadSeconds(index);
  const levels = new Map<number, ModelLevel>();
  for (const bundle of modelProfileBundles(manifest.bundles.map((entry) => entry.variable))) {
    const session = sessions.get(bundle.id);
    if (!session || !series.cellFor(session.metadata)) continue;
    const offset = sessionOffsetForLead(session, lead);
    if (offset === null) continue;
    const variables = probeVariables(session);
    const level = levels.get(bundle.level) ?? { p: bundle.level, t: null };
    if (bundle.field === "wind") {
      if (variables.length < 2) continue;
      const u = probeSeriesValues(series, [variables[0]!], [offset])[0];
      const v = probeSeriesValues(series, [variables[1]!], [offset])[0];
      if (typeof u === "number" && typeof v === "number") {
        level.u = u;
        level.v = v;
      }
    } else {
      const value = probeSeriesValues(series, variables, [offset])[0];
      if (typeof value === "number") {
        if (bundle.field === "tmp") level.t = value;
        else level.rh = value;
      }
    }
    levels.set(bundle.level, level);
  }
  const column = [...levels.values()].filter(
    (level) => level.t !== null || level.u !== undefined,
  );
  if (column.length === 0) return null;
  return {
    profile: profileFromModel(column),
    run: currentRun ? `${manifest.model} ${currentRun}` : manifest.model,
    validTime: frameValidTime(index),
  };
}

/** The row's readout at the playhead: every series' value in row order
 * ("23.5 · 18.0"), and the unit they share for the line beneath. */
function formatRowReadout(row: MeteogramRowData, index: number): { values: string; unit: string } {
  const parts: string[] = [];
  let unit = "";
  for (const [position, id] of row.spec.bundles.entries()) {
    const session = sessions.get(id);
    if (session) unit ||= displayUnit(session.variable.unit);
    const value = row.series[position]![index];
    parts.push(
      session && typeof value === "number"
        ? formatProbeValue(session.variable, displayValue(session.variable.unit, value))
        : "--",
    );
  }
  return { values: parts.join(" · "), unit };
}

/** The day marks, placed exactly as the capsule's day strip places them —
 * whole forecast days from the run, every other one on a long axis — so
 * the two read the same instants. */
function meteogramDayMarks(): DayMark[] {
  const marks: DayMark[] = [];
  const days = forecastDayCount();
  const stride = days > 6 ? 2 : 1;
  for (let day = stride; day <= days; day += stride) {
    const index = dayFrameIndex(day);
    if (index === null) continue;
    marks.push({ index, label: formatDayMark(frameValidTime(index)) });
  }
  return marks;
}

/** The meteogram rows: read every series onto the primary axis, write the
 * readouts, draw the traces. */
function renderProbeRows(series: ProbeSeries, index: number): void {
  const specs = currentMeteogramRows();
  // The aerodrome forecast is a row of the airport product, not of the run,
  // so it is appended rather than derived from the manifest — and only
  // while the pinned point has an airport with a current TAF.
  const taf = probeAirport?.history?.taf ?? null;
  if (specs.length > 0 && taf) specs.push(TAF_ROW_SPEC);
  syncProbeRowElements(specs);
  // The sparkline repeats a row when the field on screen is one of the
  // rows' bundles; then the row stands for it, marked, and the sparkline
  // gives its height back to the map.
  const active = activeSession?.id;
  const inRows = active !== undefined && specs.some((spec) => spec.bundles.includes(active));
  probePanel.canvas.hidden = inRows;
  for (const element of probeRowElements) {
    element.root.classList.toggle("is-active", active !== undefined && element.spec.bundles.includes(active));
  }
  if (specs.length === 0) return;
  const offsets = frameAxis();
  const leads = offsets.map((_, position) => frameLeadSeconds(position));
  const axis = probeObservationAxis(leads);
  const history = probeAirport?.history ?? null;
  // What the airport reported at the frame on screen, for the readouts: one
  // lookup for every row rather than one per row.
  const observedNow =
    history && axis ? nearestObservation(history.metars, frameValidTime(index)) : null;
  const rows: MeteogramRowData[] = [];
  for (const [position, spec] of specs.entries()) {
    const element = probeRowElements[position]!;
    if (spec.id === "taf") {
      const bands = taf && axis ? tafBands(taf, axis) : [];
      rows.push({ spec, series: [], bands });
      element.root.dataset.state = bands.length ? "complete" : "empty";
      // The row's readout is the period covering the frame on screen: what
      // the aerodrome is forecast to have at the valid time.
      const lead = frameLeadSeconds(index);
      const covering = bands.find((band) => lead >= band.from && lead <= band.to);
      element.value.value = covering?.label || "--";
      element.note.textContent = covering?.prob === null || covering === undefined
        ? ""
        : `PROB${covering.prob}`;
      continue;
    }
    const read = spec.bundles.map((id) => probeSessionSeries(series, sessions.get(id), leads));
    const row: MeteogramRowData = { spec, series: read.map((item) => item.values) };
    if (history && axis) {
      const marks = rowObservations(spec.id, history.metars, axis);
      if (marks.length) row.observations = marks;
    }
    let direction: number | null = null;
    const wind = spec.id === "wind" ? sessions.get(spec.bundles[0]!) : undefined;
    if (wind?.vector && series.cellFor(wind.metadata)) {
      const variables = probeVariables(wind);
      row.directions = leads.map((lead) => {
        const offset = sessionOffsetForLead(wind, lead);
        return offset === null ? null : probeWindDirection(series, variables, offset);
      });
      direction = row.directions[index] ?? null;
    }
    rows.push(row);
    // The row's state is its headline series': the stylesheet dims a row
    // still waiting, and the tests read it.
    element.root.dataset.state = read[0]!.state;
    const readout = formatRowReadout(row, index);
    element.value.value = readout.values;
    // The unit sits under the numbers, and the wind's direction beside it
    // the way it rides the lead line above: degrees the wind comes from.
    // Last comes what the airport measured within ninety minutes of this
    // frame, so the forecast and the observation read on one line.
    const observed = observedNow === null ? null : rowObservedValue(spec.id, observedNow);
    const session = sessions.get(spec.bundles[0]!);
    const reported =
      observed === null || !session
        ? null
        : `${t("soundingObserved")} ${formatProbeValue(session.variable, observed)}`;
    // The label column is the capsule's and cannot grow, so the line holds
    // two things at most. Where an observation is in hand it takes the
    // place of both the unit and the wind's direction: the unit moves up
    // to the code line for that row, and the direction is already drawn
    // as an arrow under every column of the row. The measurement is
    // nowhere else, so it is what the line keeps.
    const code = meteogramRowCode(spec);
    element.code.textContent = reported !== null && readout.unit ? `${code} · ${readout.unit}` : code;
    const parts = [
      reported !== null ? null : readout.unit,
      reported !== null || direction === null
        ? reported
        : `${String(Math.round(direction)).padStart(3, "0")}°`,
    ].filter((part): part is string => part !== null && part !== "");
    element.note.textContent = parts.join(" · ");
  }
  drawProbeRowsChart(rows, index, offsets.length);
}

function drawProbeRowsChart(rows: MeteogramRowData[], selected: number, count: number): void {
  const canvas = probePanel.rowsChart;
  const context = canvas.getContext("2d");
  if (!context) return;
  const width = canvas.clientWidth;
  const height = METEOGRAM_HEADER_HEIGHT + rows.length * METEOGRAM_ROW_HEIGHT;
  canvas.style.height = `${height}px`;
  if (width === 0) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  const styles = getComputedStyle(document.body);
  drawMeteogram(
    context,
    { width, headerHeight: METEOGRAM_HEADER_HEIGHT, rowHeight: METEOGRAM_ROW_HEIGHT },
    rows,
    {
      count,
      selected,
      dayMarks: meteogramDayMarks(),
      leadSeconds: frameAxis().map((_, position) => frameLeadSeconds(position)),
      ink: {
        ink: styles.getPropertyValue("--accent").trim() || "#54d6c7",
        muted: styles.getPropertyValue("--muted").trim() || "#8ca6b2",
      },
      font: "8px 'IBM Plex Mono', monospace",
    },
  );
}

/** A press on either chart scrubs to the frame under the pointer, the way
 * a press on the track does, and a drag keeps scrubbing until it lifts. */
function bindProbeScrub(canvas: HTMLCanvasElement): void {
  const scrub = (event: PointerEvent, gutter: number): void => {
    if (!activeVariable) return;
    const rect = canvas.getBoundingClientRect();
    const width = rect.width - gutter;
    const index = frameIndexAtX(event.clientX - rect.left - gutter, frameCount(), width);
    stopPlayback();
    generation += 1;
    trySelectFrame(index);
  };
  let pressed = false;
  canvas.addEventListener("pointerdown", (event) => {
    pressed = true;
    canvas.setPointerCapture(event.pointerId);
    scrub(event, canvas === probePanel.canvas ? probeColumnWidth() : 0);
    event.preventDefault();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (pressed) scrub(event, canvas === probePanel.canvas ? probeColumnWidth() : 0);
  });
  const release = (): void => {
    pressed = false;
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
}
bindProbeScrub(probePanel.canvas);
bindProbeScrub(probePanel.rowsChart);


/** The label column the stylesheet gives the probe panel and the capsule:
 * the sparkline's gutter, so its plot starts where the traces and the track
 * do. */
function probeColumnWidth(): number {
  const width = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--probe-column"));
  return Number.isFinite(width) ? width : 136;
}

/** The series as a sparkline: sampled frames joined, gaps left open, the
 * playhead marked. Values are quantized, so the ladder in a flat stretch is
 * the codebook's own step, not noise. */
function drawProbeChart(values: ProbeValue[], selected: number, variable: BundleVariable): void {
  const canvas = probePanel.canvas;
  const context = canvas.getContext("2d");
  if (!context || canvas.hidden) return;
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

  // The canvas runs under both columns (the stylesheet pulls it left by the
  // label column), so the plot starts where the traces and the track do,
  // and the gutter before it is where the range is written.
  const gutter = probeColumnWidth();
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

  // The range, as a scale to the left of the plot's edge: in the label
  // column, under the headline's code and below its value.
  context.fillStyle = mutedColor;
  context.textAlign = "right";
  context.fillText(formatProbeValue(variable, highest), gutter - 8, top);
  if (!flat) context.fillText(formatProbeValue(variable, lowest), gutter - 8, bottom);
}


/** The data card's format readout. The container's names are literals the
 * card has always shown; the store's goes through the dictionary like the
 * rest of the card, though as instrument text it reads "Zarr" everywhere. */
function formatReadout(format: VariableSession["format"]): string {
  if (format === "Zarr") return t("formatZarr");
  if (format === "Zarr ½") return `${t("formatZarr")} ½`;
  return format;
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
  slider.setAttribute("aria-valuetext", showingObservations() ? formatDate(valid) : `${lead}, ${formatDate(valid)}`);
  forecastLead.value = lead;
  validTime.textContent = formatDate(valid);
  validTime.dateTime = new Date(valid).toISOString();
  frameTooltip.value = frameStampLine(index);
  // Read by the tooltip and by the track's playhead, so it is set on the
  // capsule both of them sit in.
  timelinePanel.style.setProperty("--frame-progress", `${(index / Math.max(1, frameCount() - 1)) * 100}%`);
  updateTicks(index);
  updateForecastDay(index);
  scheduleProbeRender();
  syncTcTime();
  syncStationTime();
}

/** Reconfigure a slot's layer for its session's own bundle grid (poster
 * grid, full grid, and variant grids all differ) before showing a real
 * plane of that session. */
function ensureSlotGrid(slot: RasterSlot, session: VariableSession): void {
  if (slot.gridSource === session.metadata) return;
  slot.layer.configureGrid(session.metadata);
  slot.layer.setDomain(modelDomain());
  slot.gridSource = session.metadata;
}

/** The dataset's own footprint on its map projection, for a regional model
 * whose regular grid extends past it (domain.ts); null for every other. A
 * case pins its own model, so this follows whichever is selected. */
function modelDomain(): LambertDomain | null {
  return FORECAST_MODELS[selectedModelId].domain ?? null;
}

/** Same for the particle layer: adopt the vector session's grid, its u/v
 * quantization and its magnitude ceiling before feeding it planes. */
function ensureWindGrid(session: VariableSession): void {
  if (!windLayer || windLayerGridSource === session.metadata) return;
  // The pair comes off the session's own variables — the file's u and v, in
  // the order its parameter blocks put them.
  const [u, v] = session.variables;
  windLayer.configureGrid(session.metadata, u && v ? [u.id, v.id] : undefined);
  windLayer.setDomain(modelDomain());
  windLayer.setMaxSpeed(sessionMaxMagnitude(session));
  windLayer.setPace(sessionParticlePace(session));
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
  if (session.vector && view.particles) return null;
  if (isCompositeInput(session)) return null;
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
    trySelectComposite(index);
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
    for (const input of compositeSessions()) requestCompositeDecode(input, frame);
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
  for (const shown of composite?.shownKeys ?? []) displayedKeys.add(shown);
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
  if (composite?.wantedKeys.has(key) && activeFrameIndex !== null) trySelectComposite(activeFrameIndex);
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

/** The room at each end of the day-label strip that the start and the horizon
 * take, in pixels of the track: a mark centred inside it would run into
 * them. */
const TRACK_END_LABEL_PX = 48;

/** Day boundaries as marks along the track, each sitting at the fraction of
 * the axis its frame falls on. Ten of them on a 240-hour run would collide,
 * so a long axis labels every other day; a mark that would run into the start or
 * the horizon at the ends of the same strip is dropped, since those already
 * name both — measured against the track, which is a third as wide on a
 * phone. Leaves the active mark where the playhead is. */
function buildForecastDays(): void {
  forecastDays.replaceChildren();
  const days = forecastDayCount();
  const stride = days > 6 ? 2 : 1;
  const lastIndex = Math.max(1, frameCount() - 1);
  const edge = Math.max(4, (TRACK_END_LABEL_PX / Math.max(1, forecastDays.clientWidth || 680)) * 100);
  for (let day = stride; day <= days; day += stride) {
    const index = dayFrameIndex(day);
    if (index === null) continue;
    const percent = (index / lastIndex) * 100;
    if (percent < edge || percent > 100 - edge) continue;
    const mark = document.createElement("time");
    mark.className = "forecast-day";
    mark.dataset.day = String(day);
    mark.style.left = `${percent.toFixed(2)}%`;
    const valid = frameValidTime(index);
    mark.dateTime = new Date(valid).toISOString();
    mark.textContent = formatDayMark(valid);
    forecastDays.append(mark);
  }
  updateForecastDay(activeFrameIndex ?? Number(slider.value));
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
  for (const button of fieldList.querySelectorAll("button")) button.disabled = disabled;
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
  /** The lines group's ALONE switch: pressed while the lines are the whole
   * view, and the way there and back. */
  alone?: boolean;
}

/** The level row's groups for the composition on screen: the fill's family
 * when it has more than one published surface, then the lines. The lines
 * group is on the row whenever the run has a surface to chart, with no
 * member pressed while the lines are off — so the overlay is one press
 * away, and one press back after it was taken off — and ALONE after the
 * surfaces, pressed while the lines are the view itself. */
function levelGroups(): LevelGroup[] {
  if (!manifest) return [];
  const run = manifest;
  const groups: LevelGroup[] = [];
  const fill = view.field;
  const fillFamily = fill === null ? null : familyOf(fill);
  if (fill !== null && fillFamily !== null) {
    // The row answers one question — which surface — so a family's
    // variants (sea ice cover and thickness) are not on it; the field
    // sheet lists those.
    const levels = familyLevels(fillFamily, (id) => hasBundle(run, id)).filter((id) => hasBundle(run, id));
    if (levels.length > 1 && levels.includes(fill)) {
      groups.push({ caption: t("levelCaption"), slot: "fill", members: levels, active: fill });
    }
  }
  const surfaces = PRESSURE_BUNDLE_IDS.filter((id) => hasBundle(run, id));
  if (surfaces.length > 0 || view.lines !== null) {
    groups.push({ caption: t("linesCaption"), slot: "lines", members: surfaces, active: view.lines, alone: fill === null });
  }
  return groups;
}

/** The field to put back when the lines-alone view is left: the one last
 * on screen, else the dataset's own default. */
function fieldToRestore(): ForecastBundleId {
  return lastField ?? activeCase?.defaultVariable ?? modelDefaultVariable(selectedModelId, DEFAULT_VARIABLE);
}

/** Leave the lines-alone view with the field put back under the lines. */
function restoreField(): void {
  void activateComposition({ fill: fieldToRestore(), lines: view.lines });
}

/** Drop the field and leave the lines by themselves, charting the surface
 * on screen or the preferred one when none is drawn yet. */
function showLinesAlone(): void {
  if (view.field !== null) lastField = view.field;
  void activateComposition({ fill: null, lines: view.lines ?? preferredPressureVariable() });
}

/** The visually hidden name of one level button: the instrument code and its
 * gloss, the same shape the rail tiles carry. */
function levelButtonName(id: ForecastBundleId): [string, string] {
  if (isPressureBundle(id)) {
    return id === "prmsl" ? ["MSLP", t("varPressure")] : [`${bundleLevel(id)}MB`, t("varHeight")];
  }
  const family = familyOf(id);
  if (family !== null && bundleLevel(id) === null) {
    const { code, glossKey, levels, variants } = FAMILIES[family];
    // A listed member (the cloud layers) is named for itself; a surface
    // member repeats its family tile's gloss.
    const gloss = levels || variants || glossKey === null ? familyLabel(id) : t(glossKey);
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
      const removable = group.slot === "lines" && id === group.active && view.field !== null;
      if (removable) {
        button.classList.add("is-removable");
        button.title = t("linesRemoveTitle");
      }
      button.addEventListener("click", () => {
        // A level changes its own slot: the fill's family member, or the
        // lines wherever they are — the view, or the chart over a field.
        if (group.slot === "lines") {
          if (removable) void activateComposition({ fill: view.field, lines: null });
          else if (isPressureBundle(id)) void activateComposition({ fill: view.field, lines: id });
        } else {
          void activateComposition({ fill: id, lines: view.lines });
        }
      });
      container.append(button);
    }
    if (group.alone !== undefined) {
      // ALONE: a switch after the surfaces, not a surface. On, the field
      // goes and the lines are the view; off, the field last on screen
      // comes back under them (the two slots are never both empty).
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.alone = "";
      button.dataset.slot = group.slot;
      button.disabled = switchingVariable;
      button.setAttribute("aria-pressed", String(group.alone));
      button.title = t("levelAloneTitle");
      const glyph = document.createElement("b");
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = "ALONE";
      const name = document.createElement("span");
      name.className = "rail-name";
      const codeSpan = document.createElement("span");
      codeSpan.textContent = "ALONE";
      const glossSmall = document.createElement("small");
      glossSmall.textContent = t("levelAlone");
      name.append(codeSpan, " ", glossSmall);
      button.append(glyph, name);
      button.addEventListener("click", () => {
        if (view.field === null) restoreField();
        else showLinesAlone();
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
  if (UNTILED_BUNDLE_IDS.includes(id)) return true;
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

/** A generic tile for a field on screen that this build has no tile
 * written for: the stacked-layers icon, lettered with the id's initial. */
function unknownRailTile(id: ForecastBundleId): HTMLButtonElement {
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
    void activateComposition({ fill: id, lines: view.lines });
  });
  return button;
}

/** Whether a written field tile has anything to show on a run: the family's
 * published members for a family tile, the bundle itself otherwise. */
function fieldTileShipped(button: HTMLButtonElement, run: ForecastManifest): boolean {
  const id = button.dataset.variable!;
  const family = button.dataset.family as IsobaricFamily | undefined;
  if (family === undefined) return hasBundle(run, id);
  return familyMembers(family, (member) => hasBundle(run, member)).some((member) => hasBundle(run, member));
}

/** Whether a written field tile stands for the field on screen: its
 * family's member, or the bundle itself. */
function fieldTileCurrent(button: HTMLButtonElement): boolean {
  const fill = view.field;
  if (fill === null) return false;
  const family = button.dataset.family as IsobaricFamily | undefined;
  return family === undefined ? button.dataset.variable === fill : familyOf(fill) === family;
}

/** A family tile's gloss follows the member on screen: TEMP reads 2M on
 * the surface and 850 on the 850 hPa member, ICE reads COVER or THICK.
 * The gloss is the localized word (the markup's, restored here) while the
 * family is not on screen or its surface member is, and the member's
 * instrument code otherwise — English in every locale, as the level row
 * is. The sheet's rows read it off the tile. */
function syncFamilyGloss(button: HTMLButtonElement, current: boolean): void {
  const family = button.dataset.family as IsobaricFamily | undefined;
  const gloss = button.querySelector<HTMLElement>(".rail-name > small");
  if (family === undefined || !gloss) return;
  const fill = view.field;
  const info = FAMILIES[family];
  const followed = current && fill !== null && fill !== info.surface ? levelCode(fill) : null;
  const key = gloss.dataset.i18n as MessageKey | undefined;
  gloss.textContent = followed ?? (key ? t(key) : gloss.textContent);
}

/** The rail's field section for the run and the field on screen: a core
 * tile (`FORECAST_MODELS[].railCore`) shows whenever the run ships it, any
 * other written tile only while its field is on screen, a field this build
 * has no tile for gets a generic one while it is, and MORE shows when the
 * sheet reaches a field the core tiles do not. The sheet's rows are
 * rebuilt with it. */
function syncFieldTiles(run: ForecastManifest): void {
  if (!fieldSection) return;
  const core = modelRailCore(selectedModelId);
  for (const button of fieldTiles()) {
    const isCore = core.includes(button.dataset.variable!);
    if (isCore) button.dataset.core = "";
    else delete button.dataset.core;
    const current = fieldTileCurrent(button);
    button.hidden = !fieldTileShipped(button, run) || !(isCore || current);
    syncFamilyGloss(button, current);
  }
  const fill = view.field;
  for (const stale of fieldSection.querySelectorAll<HTMLButtonElement>("button[data-unknown]")) {
    if (stale.dataset.variable !== fill) stale.remove();
  }
  if (fill !== null && !railTileStandsFor(fill) && !fieldSection.querySelector(`button[data-unknown][data-variable="${fill}"]`)) {
    fieldSection.append(unknownRailTile(fill));
  }
  const rows = fieldSheetRows(run);
  fieldMore.hidden = rows.every((row) => row.core);
  if (fieldMore.hidden) fieldSheetControl.close();
  renderFieldSheet(rows);
  syncRailDensity();
}

/** The sheet's groups, in the order they are listed (`FIELD_GROUPS`, from
 * the variable table, which also says which group each field is in), then
 * a last one for every bundle the run publishes that no tile stands for. */
type SheetGroup = FieldGroup | "other";
const FIELD_GROUP_LABEL: Record<SheetGroup, MessageKey> = {
  temperature: "fieldGroupTemperature",
  moisture: "fieldGroupMoisture",
  wind: "fieldGroupWind",
  dynamics: "fieldGroupDynamics",
  radiation: "fieldGroupRadiation",
  ocean: "fieldGroupOcean",
  satellite: "fieldGroupSatellite",
  other: "fieldGroupOther",
};

interface FieldSheetRow {
  group: SheetGroup;
  /** The bundle a press opens: the family's preferred member for a family
   * tile, the bundle itself otherwise. */
  id: ForecastBundleId;
  /** A family's published surfaces and variants, one chip each; none for
   * a single field or a family with one member on this run. */
  members: ForecastBundleId[];
  /** The written tile the row is drawn from; null for a field this build
   * has no tile for. */
  tile: HTMLButtonElement | null;
  /** Whether the rail carries the field's tile whatever is on screen. */
  core: boolean;
  current: boolean;
}

/** One row per field the run publishes, grouped and ordered as the sheet
 * lists them. */
function fieldSheetRows(run: ForecastManifest): FieldSheetRow[] {
  const core = modelRailCore(selectedModelId);
  const tiles = new Map(fieldTiles().map((button) => [button.dataset.variable!, button]));
  const rows: FieldSheetRow[] = [];
  // The written tiles, in the table's order, each under its field's group.
  const tileIds = variableIds().filter((id) => tiles.has(id));
  for (const group of FIELD_GROUPS) {
    for (const id of tileIds) {
      if (variableSpec(id)?.group !== group) continue;
      const tile = tiles.get(id)!;
      if (!fieldTileShipped(tile, run)) continue;
      const family = tile.dataset.family as IsobaricFamily | undefined;
      const members = family === undefined ? [] : familyMembers(family, (member) => hasBundle(run, member)).filter((member) => hasBundle(run, member));
      rows.push({
        group,
        id: family === undefined ? id : preferredFamilyMember(family),
        members: members.length > 1 ? members : [],
        tile,
        core: core.includes(id),
        current: fieldTileCurrent(tile),
      });
    }
  }
  for (const bundle of run.bundles) {
    const id = bundle.variable;
    if (railTileStandsFor(id)) continue;
    rows.push({ group: "other", id, members: [], tile: null, core: false, current: view.field === id });
  }
  return rows;
}

/** Write the sheet's rows: a heading per group that has one, then each
 * field as its rail icon, its code with its gloss, and its full name, the
 * one on screen checked — and under a family, a chip per surface and
 * variant the run publishes, the one on screen pressed: the second way
 * onto a level, and the main way onto a variant. */
function renderFieldSheet(rows: FieldSheetRow[]): void {
  const nodes: HTMLElement[] = [];
  let heading: SheetGroup | null = null;
  for (const row of rows) {
    if (row.group !== heading) {
      heading = row.group;
      const title = document.createElement("p");
      title.className = "field-group-heading";
      title.textContent = t(FIELD_GROUP_LABEL[row.group]);
      nodes.push(title);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.field = row.id;
    button.disabled = variableButtonsDisabled || switchingVariable;
    button.setAttribute("aria-pressed", String(row.current));
    const icon = document.createElement("span");
    icon.className = "field-row-icon";
    icon.setAttribute("aria-hidden", "true");
    const drawn = row.tile?.querySelector("svg.rail-icon");
    icon.append(drawn ? (drawn.cloneNode(true) as SVGSVGElement) : unknownRailIcon());
    const text = document.createElement("span");
    text.className = "field-row-text";
    const code = document.createElement("span");
    code.className = "field-row-code";
    const codeWord = document.createElement("span");
    codeWord.textContent = row.tile?.querySelector(".rail-name > span")?.textContent ?? row.id.toUpperCase();
    const gloss = document.createElement("small");
    gloss.textContent = row.tile?.querySelector(".rail-name > small")?.textContent ?? t("varUnknownLayer");
    code.append(codeWord, gloss);
    const label = document.createElement("span");
    label.className = "field-row-label";
    label.textContent = row.tile?.dataset.tip ?? row.id;
    text.append(code, label);
    const check = document.createElement("span");
    check.className = "model-check";
    check.setAttribute("aria-hidden", "true");
    button.append(icon, text, check);
    button.addEventListener("click", () => {
      fieldSheetControl.close();
      void activateComposition({ fill: row.id, lines: view.lines });
    });
    if (row.members.length === 0) {
      nodes.push(button);
      continue;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "field-row";
    const chips = document.createElement("div");
    chips.className = "field-chips";
    chips.setAttribute("role", "group");
    for (const member of row.members) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.dataset.member = member;
      chip.disabled = variableButtonsDisabled || switchingVariable;
      chip.setAttribute("aria-pressed", String(member === view.field));
      chip.textContent = levelCode(member);
      chip.title = familyLabel(member);
      chip.addEventListener("click", () => {
        fieldSheetControl.close();
        void activateComposition({ fill: member, lines: view.lines });
      });
      chips.append(chip);
    }
    wrapper.append(button, chips);
    nodes.push(wrapper);
  }
  fieldList.replaceChildren(...nodes);
}

/** A rail section with nothing to show is hidden whole, and the rule
 * between sections goes with it. */
function syncRailSections(): void {
  if (!variableRail) return;
  for (const section of variableRail.querySelectorAll<HTMLElement>(".rail-section")) {
    section.hidden = ![...section.querySelectorAll<HTMLButtonElement>("button")].some((button) => !button.hidden);
  }
}

/** A tile's height and the gap under it at the rail's full size, and the
 * rule between two sections with its margins: what the rail would need for
 * its visible tiles, against the box it has. */
const RAIL_TILE_PITCH = 44 + 6;
const RAIL_RULE_PITCH = 1 + 2 + 6;

/** Whether the rail's visible tiles fit its box at full size; past that
 * the stylesheet's dense variant takes over (see `.variable-rail`).
 * Measured from the count rather than the rendered height, so the dense
 * tiles fitting never argues the rail back out of dense. Nothing about
 * which tiles show changes — only their height and icon. */
function syncRailDensity(): void {
  if (!variableRail) return;
  syncRailSections();
  const sections = [...variableRail.querySelectorAll<HTMLElement>(".rail-section")].filter((section) => !section.hidden);
  const visible = sections.reduce(
    (count, section) => count + [...section.querySelectorAll<HTMLButtonElement>("button")].filter((button) => !button.hidden).length,
    0,
  );
  const needed = visible * RAIL_TILE_PITCH + Math.max(0, sections.length - 1) * RAIL_RULE_PITCH;
  const box = variableRail.clientHeight - 8;
  if (box > 0 && needed > box) variableRail.dataset.dense = "";
  else delete variableRail.dataset.dense;
  syncRailFade();
}

/** Every pressed state on the rail, from the view: a field tile for the
 * field on screen (its family's, for a family tile), the pressure switch
 * while lines are drawn, the particles, the experiment's derived layers,
 * and the two station products where their tiles show. The one exception
 * is the storm tile, pressed while tracks are drawn, which `applyTcView`
 * sets from what actually loaded. */
function syncRail(): void {
  const fill = view.field;
  const fillFamily = fill === null ? null : familyOf(fill);
  for (const button of variableButtons()) {
    const family = button.dataset.family as IsobaricFamily | undefined;
    const pressed = button.dataset.group === "pressure"
      ? view.lines !== null
      : family !== undefined
        ? fillFamily === family
        : button.dataset.variable === fill;
    button.setAttribute("aria-pressed", String(pressed));
    // A rail taller than its box scrolls, and the tile on screen belongs in
    // view — clear of the fade, which is what the rail's scroll padding is.
    if (pressed && !button.hidden) button.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  particlesToggle.setAttribute("aria-pressed", String(view.particles));
  for (const button of derivedButtons()) {
    button.setAttribute("aria-pressed", String(view.derived[button.dataset.derived as "inflow" | "front"]));
  }
  soundingTile.setAttribute("aria-pressed", String(!soundingTile.hidden && view.marks.stations.soundings));
  airportTile.setAttribute("aria-pressed", String(!airportTile.hidden && view.marks.stations.airports));
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
// Which day marks fit beside the start and the horizon depends on the track's
// width, so a resize lays them out again.
let forecastDaysWidth = 0;
new ResizeObserver(() => {
  const width = forecastDays.clientWidth;
  if (width === forecastDaysWidth) return;
  forecastDaysWidth = width;
  if (metadata) buildForecastDays();
}).observe(forecastDays);
if (variableRail) new ResizeObserver(syncRailDensity).observe(variableRail);

/** The legend bar's gradient for a field whose key is not in the stylesheet:
 * the upper-air fills, the surface diagnostics, solar radiation and every
 * unrecognized field read theirs off the palette they are actually drawn
 * with, over the range the legend's ticks span. */
function legendGradientFor(session: VariableSession): string {
  // A field whose legend bar is a hand-written gradient in the stylesheet
  // (`body[data-variable=…] .legend-bar`) leaves it there; every other reads
  // its bar off the palette it is actually drawn with.
  const chartId = session.chartId;
  if (chartId !== null && variableSpec(chartId)?.legendGradient === "stylesheet") return "";
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
  const spec = session.chartId === null ? null : variableSpec(session.chartId);
  if (spec) {
    return { code: spec.code, title: spec.title, bufferTitle: spec.bufferTitle, label: spec.label(), legend: spec.legend() };
  }
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
          title: t(showingObservations() ? "pageTitleLiveObservation" : "pageTitleLive", {
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
  legendUnit.textContent = displayUnit(session.variable.unit);
  legendLabels.replaceChildren(...ui.legend.map((label) => {
    const span = document.createElement("span");
    span.textContent = label;
    return span;
  }));
  legendBar.style.background = legendGradientFor(session);
  syncDerivedLegend();
  if (experimentEnabled && session.chartId === "prate") {
    const key = steppedPrecipitationLegend();
    legendBar.style.background = key.gradient;
    legendLabels.replaceChildren(...key.labels.map((label) => {
      const span = document.createElement("span");
      span.textContent = label;
      return span;
    }));
  }
  // Each family remembers the member last on screen, so its rail tile
  // reopens it; the field itself is remembered for the way back from the
  // lines-alone view.
  const lines = view.lines;
  if (lines !== null) lastFamilyMember.set("hgt", lines);
  const fillFamily = view.field === null ? null : familyOf(view.field);
  if (view.field !== null && fillFamily !== null) lastFamilyMember.set(fillFamily, view.field);
  if (view.field !== null) lastField = view.field;
  // The level row: the fill's surfaces when its family has several, and the
  // lines' whenever the run has a surface to chart, with ALONE for the
  // lines-alone view.
  renderLevelRow();
  // The particle overlay belongs to the vector fields, so its switch appears
  // with them.
  particlesToggle.hidden = !session.vector && !compositeFlowPublished();
  // The field section follows the field on screen: its tile shows beside
  // the core ones while it is there.
  if (manifest) syncFieldTiles(manifest);
  syncRail();
  syncRailDensity();
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

/** The share of the view a regional model's region must fill for the
 * viewer to count as already looking at it. Below this the region is a
 * patch on a wider map — the world view overlaps every region and shows
 * none of them — and opening the model frames it; at or above it, a view
 * zoomed onto some part of the region stays where it is. */
const REGION_IN_VIEW_SHARE = 0.5;

/** Move the camera onto a regional model's own region
 * (`FORECAST_MODELS[].region`) unless the view is already over it; a global
 * model has no region. Unlike a case's camera, nothing is pinned: the
 * limits are computed only to pick the framing, and the world around the
 * region stays reachable, empty as it is. */
function frameModelRegion(): void {
  const region = FORECAST_MODELS[selectedModelId].region;
  if (!region) return;
  const bounds = map.getBounds();
  const view = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()] as const;
  if (regionShareOfView(region, view) >= REGION_IN_VIEW_SHARE) return;
  const canvas = map.getCanvas();
  const limits = caseCameraLimits([...region], {
    width: canvas.clientWidth,
    height: canvas.clientHeight,
  });
  map.jumpTo({ center: limits.center, zoom: limits.minZoom });
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
  const search = searchForView(
    view,
    window.location.search,
    { model: selectedModelId, caseId: activeCase?.id ?? null, experimentEnabled, particlesChosen },
    DEFAULT_VARIABLE,
  );
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
 * viewer is actually reading. `?res=full` still pins every session.
 *
 * A probe session (the meteogram's rows) is the primary's tier opened
 * quietly on the streaming path alone: it exists to read one cell's series
 * out of one tile, so it never takes the video path and never downloads a
 * bundle whole — where the origin cannot serve ranges it is not opened at
 * all. The session it makes is otherwise the one a layer would have made,
 * and the rail reuses it should the viewer switch to that field. */
function loadVariable(
  variableId: ForecastBundleId,
  sequence: number,
  role: "primary" | "overlay" | "probe" = "primary",
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
    const quiet = role !== "primary";
    const variant = pickBundleVariant(
      descriptor.variants,
      neededGridWidth(),
      slowConnection(),
      overlay && resolutionPreference !== "full" ? "half" : resolutionPreference,
      bundleLongitudeSpan(descriptor),
    );
    const video = h264Enabled && role !== "probe" ? descriptor.video : undefined;
    // Opted in, the video path must still earn its bytes — prefer it only
    // when the stream is not larger than the bundle it replaces (lossless
    // H.264 wins that comparison for tmp2m but loses it for prate; the
    // manifest's byteLengths decide, so a future lossy tier flips this
    // automatically).
    if (
      !variant &&
      video &&
      video.byteLength <= deliveryBytes(descriptor) &&
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
          quiet,
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
      const container = containerOf(target);
      // The store is the everyday path wherever the tier carries one
      // (`?backend=xue` asks for the container instead), streamed over
      // ranges. An origin that serves none sends the session to the
      // container — streamed, else downloaded whole — and only an entry
      // that ships no container at all opens its store by whole objects,
      // one shard (one temporal group) per GET: what a whole bundle
      // download costs, group by group. A probe session is streamed or
      // nothing either way.
      const store = zarrStoreFor(dataBackend, descriptor, variant);
      const storeRoot = store && manifestUrl ? zarrRootUrl(store.path, manifestUrl) : null;
      const storeStreams =
        store !== undefined &&
        storeRoot !== null &&
        (await supportsRangeRequests(zarrObjectUrl(storeRoot, "zarr.json", store.crc32)));
      if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
      if (store !== undefined && storeRoot !== null && (storeStreams || !container)) {
        if (!storeStreams && role === "probe") {
          throw new Error("range requests unsupported; a probe session never downloads a whole shard");
        }
        streaming = true;
        channel = spawnZarrWorker();
        channel.onerror = (event) => showError(event.message || t("workerStartFailed"));
        initMessage = zarrInitMessage(storeRoot, store, variableId, storeStreams);
        downloadedBytes = 0;
        format = variant ? "Zarr ½" : "Zarr";
        totalBytes = store.byteLength;
      } else if (container) {
        const url = artifactUrl(container.path, container.crc32);
        streaming = await supportsRangeRequests(url);
        if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
        if (streaming) {
          channel = spawnWorker();
          initMessage = {
            type: "init-stream",
            url,
            byteLength: container.byteLength,
            variableKey: variableId,
          };
          downloadedBytes = 0;
        } else if (role === "probe") {
          throw new Error("range requests unsupported; a probe session never downloads a whole bundle");
        } else {
          const initBuffer = await downloadBundle(container, sequence, quiet);
          if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
          channel = spawnWorker();
          initMessage = { type: "init", buffer: initBuffer };
          transfer = [initBuffer];
          downloadedBytes = container.byteLength;
        }
        format = variant ? "Xue ½" : "Xue";
        totalBytes = container.byteLength;
      } else {
        // The validator admits no entry without one delivery or the other.
        throw new Error(t("manifestMissingBundle", { id: variableId }));
      }
    }

    if (!quiet) say(loadStatus, streaming ? "readingIndex" : "initializingDecoder");
    let opened: Awaited<ReturnType<typeof initializeChannel>>;
    try {
      opened = await initializeChannel(channel, initMessage, transfer, sequence);
    } catch (error) {
      // A store this shell cannot open on a run newer than itself is, like
      // a manifest it refuses, most likely one a newer shell reads: the
      // profile's shard layout changed once already. Same cure, same guard.
      if (format.startsWith("Zarr") && role === "primary" && !activeCase) {
        throw new StoreRejectedError(error instanceof Error ? error.message : String(error));
      }
      throw error;
    }
    const { worker: sessionWorker, metadata: bundleMetadata, tiles } = opened;
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
  // Degrees east of the grid origin, signed: a view west of a regional
  // window is at a negative cell, not most of the way around the world, and
  // a window past the antimeridian (a satellite disk to 200.7) takes a view
  // spelled at -170 as one at 190 (`tiles.ts` does the same).
  const offset = wrap(centerLongitude - spanLongitude / 2 - grid.firstLongitude + 180, 360) - 180;
  let column0 = Math.floor(offset / grid.longitudeStep);
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
    domain: modelDomain() ?? undefined,
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
// ---------------------------------------------------------------------------
// Tropical cyclone marks: the product beside the runs (docs/tc.md), drawn
// as MapLibre GeoJSON layers over whatever fill and lines are on screen.
// They take no session and no worker and never gate the playhead; the
// playhead's valid time is what they follow.

let tcLoaded: LoadedTcIndex | null = null;
/** Whether the C-level systems — found by a model, tracked by no centre —
 * are listed and drawn. A session choice, not carried in the URL. */
let tcPotential = false;
/** Whether the best track is drawn beside the forecasts. A session choice,
 * not carried in the URL; the focused storm, the hidden switch, the agency
 * and model keys switched on and the members are `view.marks.tc`. */
let tcBest = true;
const tcStorms = new Map<string, { crc32: string; storm: TcStorm }>();
/** Storm files in flight, keyed by id and crc, so two views asked for at
 * once (the run's initialize and the product's poll) share one fetch. */
const tcStormLoads = new Map<string, Promise<void>>();
let tcLayers: StormLayers | null = null;
let tcApplySequence = 0;
let tcDrawn: TcStorm[] = [];
/** Whether the storm selected from a link or the sheet has been flown to.
 * Once per selection: a returning poll never moves the camera. */
let tcFramed: string | null = null;

/** The live index, or none: a data root without the product hides the
 * tile, and a transient failure keeps whatever was loaded before. */
async function loadTc(): Promise<void> {
  if (document.hidden && tcLoaded !== null) return;
  try {
    const loaded = await fetchTcIndex(dataBaseUrl());
    tcLoaded = loaded;
  } catch {
    // A poll that fails leaves the previous hour drawn.
    if (tcLoaded === null) return;
  }
  if (tcLoaded && view.marks.tc.storm !== null) {
    const resolved = resolveTcStormId(tcLoaded.index, view.marks.tc.storm);
    // A storm that has left the product falls back to the overview rather
    // than to an empty map.
    view.marks.tc.storm = resolved;
    if (resolved === null) syncUrl();
  }
  void applyTcView();
}

/** The card open on a clicked storm point, one at a time. */
let tcCard: Popup | null = null;

function tcPointAt(point: { x: number; y: number }): { data: TcPointData; lngLat: [number, number] } | null {
  if (!tcLayers) return null;
  const layers = TC_CLICKABLE_LAYERS.filter((id) => map.getLayer(id) !== undefined);
  if (!layers.length) return null;
  const box: [[number, number], [number, number]] = [
    [point.x - 6, point.y - 6],
    [point.x + 6, point.y + 6],
  ];
  for (const feature of map.queryRenderedFeatures(box, { layers })) {
    const data = pointDataOf(feature);
    if (data) return { data, lngLat: [data.lon, data.lat] };
  }
  return null;
}

function showTcCard(data: TcPointData, lngLat: [number, number]): void {
  closeTcCard();
  tcCard = new Popup({ closeButton: false, closeOnClick: false, className: "tc-popup", maxWidth: "300px", offset: 12 })
    .setLngLat(lngLat)
    .setDOMContent(buildTcCard(data, { formatTime: (time) => formatDate(time) }))
    .addTo(map);
}

function closeTcCard(): void {
  tcCard?.remove();
  tcCard = null;
}

function ensureTcLayers(): StormLayers | null {
  if (!mapStyleReady) return null;
  if (!tcLayers) tcLayers = new StormLayers(map);
  const before = (map.getStyle().layers ?? []).find((entry) => entry.type === "symbol")?.id;
  tcLayers.ensure(before);
  tcLayers.setInk(document.body.dataset.ground === "dark");
  return tcLayers;
}

/** The index entries the view draws: the focused storm, else every named
 * and every tracked-but-unnumbered system, plus the model-only ones when
 * asked for. */
function tcEntriesToDraw(): TcIndexEntry[] {
  if (!tcLoaded || view.marks.tc.off || activeCase !== null) return [];
  const storms = tcLoaded.index.storms;
  if (view.marks.tc.storm !== null) return storms.filter((entry) => entry.id === view.marks.tc.storm);
  return storms.filter((entry) => entry.level !== "C" || tcPotential);
}

/** The valid time the marks are drawn at: the playhead's, or now before a
 * run has loaded. */
function syncTcTime(): void {
  if (!tcLayers) return;
  const index = activeFrameIndex ?? requestedFrameIndex;
  tcLayers.setTime(metadata && index !== null ? frameValidTime(index) : Date.now());
}

function tcAgencyKeys(): string[] {
  const keys: string[] = [];
  for (const storm of tcDrawn) for (const key of Object.keys(storm.agencies)) if (!keys.includes(key)) keys.push(key);
  return keys;
}

function tcModelKeys(): string[] {
  const keys = new Set<string>();
  for (const storm of tcDrawn) for (const key of Object.keys(storm.models)) keys.add(key);
  const known = Object.keys(TC_MODELS);
  return [...keys].sort((a, b) => (known.indexOf(a) + 1 || 99) - (known.indexOf(b) + 1 || 99));
}

async function applyTcView(): Promise<void> {
  const sequence = ++tcApplySequence;
  const entries = tcEntriesToDraw();
  const hidden = activeCase !== null || !tcLoaded || tcLoaded.index.storms.length === 0;
  if (tcTile.hidden !== hidden) {
    tcTile.hidden = hidden;
    syncRailDensity();
  }
  const loaded = tcLoaded;
  if (loaded) {
    await Promise.all(
      entries
        .filter((entry) => tcStorms.get(entry.id)?.crc32 !== entry.crc32)
        .map((entry) => {
          const key = `${entry.id}?${entry.crc32}`;
          let load = tcStormLoads.get(key);
          if (!load) {
            load = fetchTcStorm(loaded, entry)
              .then((storm) => {
                tcStorms.set(entry.id, { crc32: entry.crc32, storm });
              })
              .catch((error: unknown) => {
                console.warn(`tc: ${entry.id} not loaded`, error);
              })
              .finally(() => {
                tcStormLoads.delete(key);
              });
            tcStormLoads.set(key, load);
          }
          return load;
        }),
    );
  }
  if (sequence !== tcApplySequence) return;
  tcDrawn = entries.map((entry) => tcStorms.get(entry.id)?.storm).filter((storm): storm is TcStorm => storm !== undefined);
  const layers = ensureTcLayers();
  const views: StormView[] = tcDrawn.map((storm) => ({
    storm,
    agencies: new Set(Object.keys(storm.agencies).filter((key) => view.marks.tc.agencies === null || view.marks.tc.agencies.includes(key))),
    models: new Set(Object.keys(storm.models).filter((key) => view.marks.tc.models === null || view.marks.tc.models.includes(key))),
    members: view.marks.tc.members,
    best: tcBest,
  }));
  layers?.setViews(views);
  syncTcTime();
  closeTcCard();
  tcTile.setAttribute("aria-pressed", String(views.length > 0));
  renderTcSheet();
  if (view.marks.tc.storm !== null && tcFramed !== view.marks.tc.storm && tcDrawn.length === 1) {
    tcFramed = view.marks.tc.storm;
    const bounds = stormBounds(tcDrawn[0]!);
    if (bounds) map.fitBounds(bounds, { padding: 80, maxZoom: 6, duration: 900 });
  }
}

/** Turn one key on or off within a "null means all" set. */
function tcToggleKey(current: readonly string[] | null, available: string[], key: string, on: boolean): readonly string[] | null {
  const next = new Set(current ?? available);
  if (on) next.add(key);
  else next.delete(key);
  return available.every((item) => next.has(item)) ? null : [...next];
}

function renderTcSheet(): void {
  renderTcPanel(
    tcList,
    {
      index: tcLoaded?.index ?? null,
      selected: view.marks.tc.storm,
      hidden: view.marks.tc.off,
      potential: tcPotential,
      agencies: new Set(tcAgencyKeys().filter((key) => view.marks.tc.agencies === null || view.marks.tc.agencies.includes(key))),
      models: new Set(tcModelKeys().filter((key) => view.marks.tc.models === null || view.marks.tc.models.includes(key))),
      members: view.marks.tc.members,
      best: tcBest,
    },
    { agencies: tcAgencyKeys(), models: tcModelKeys() },
    {
      onSelect(id) {
        view.marks.tc.storm = id;
        view.marks.tc.off = false;
        tcFramed = null;
        syncUrl();
        tcSheetControl.close();
        void applyTcView();
      },
      onAgency(id, on) {
        view.marks.tc.agencies = tcToggleKey(view.marks.tc.agencies, tcAgencyKeys(), id, on);
        syncUrl();
        void applyTcView();
      },
      onModel(id, on) {
        view.marks.tc.models = tcToggleKey(view.marks.tc.models, tcModelKeys(), id, on);
        syncUrl();
        void applyTcView();
      },
      onMembers(on) {
        view.marks.tc.members = on;
        syncUrl();
        void applyTcView();
      },
      onBest(on) {
        tcBest = on;
        void applyTcView();
      },
      onPotential(on) {
        tcPotential = on;
        void applyTcView();
      },
      onHidden(hidden) {
        view.marks.tc.off = hidden;
        syncUrl();
        void applyTcView();
      },
    },
  );
}

// ---------------------------------------------------------------------------
// Station marks: the two point products beside the runs and the storms
// (docs/sounding.md, docs/airport.md), drawn as MapLibre circle layers
// over whatever fill and lines are on screen. Like the storm tracks they
// take no session and no worker and never gate the playhead — the
// playhead's valid time only decides which marks are drawn faint — and
// like them, a root that publishes neither costs one 404 per poll and
// nothing else.
//
// Both are off until asked for: five thousand airport marks on the
// default view are a texture over the field rather than a reading of it,
// so each is its own rail tile, and the press is what `?stations=` then
// carries. Nothing is remembered in storage: the link is the memory.

let soundingLoaded: LoadedSoundingIndex | null = null;
let airportLoaded: LoadedAirportIndex | null = null;
/** Whether a poll has run at all, so a tab that opened hidden still loads
 * the products once while a later poll on a hidden one does not. */
let stationsPolled = false;
let stationLayers: StationLayers | null = null;
/** The card open on a clicked mark, one at a time — and the storm card's
 * sibling: opening either closes the other. */
let stationCard: Popup | null = null;

/** The live indexes, or none. Each product is fetched on its own: one
 * that is not published yet, or an hour that failed, leaves the other
 * drawn. */
async function loadStations(): Promise<void> {
  if (document.hidden && stationsPolled) return;
  const base = dataBaseUrl();
  const [soundings, airports] = await Promise.allSettled([fetchSoundingIndex(base), fetchAirportIndex(base)]);
  stationsPolled = true;
  // A poll that fails leaves the previous issue drawn; only an answer
  // replaces it.
  if (soundings.status === "fulfilled") soundingLoaded = soundings.value;
  if (airports.status === "fulfilled") airportLoaded = airports.value;
  applyStationView();
}

function ensureStationLayers(): StationLayers | null {
  if (!mapStyleReady) return null;
  if (!stationLayers) stationLayers = new StationLayers(map);
  const before = (map.getStyle().layers ?? []).find((entry) => entry.type === "symbol")?.id;
  stationLayers.ensure(before);
  stationLayers.setInk(document.body.dataset.ground === "dark");
  return stationLayers;
}

/** The tiles, the marks and the card, from what has loaded and what is
 * switched on. A case is a fixed past over one region; the live stations
 * have no place over it, so both tiles go away with it. */
function applyStationView(): void {
  const hidden = activeCase !== null;
  const soundingAvailable = soundingLoaded !== null && !hidden;
  const airportAvailable = airportLoaded !== null && !hidden;
  let railChanged = false;
  if (soundingTile.hidden === soundingAvailable) {
    soundingTile.hidden = !soundingAvailable;
    railChanged = true;
  }
  if (airportTile.hidden === airportAvailable) {
    airportTile.hidden = !airportAvailable;
    railChanged = true;
  }
  if (railChanged) syncRailDensity();
  const drawSoundings = soundingAvailable && view.marks.stations.soundings;
  const drawAirports = airportAvailable && view.marks.stations.airports;
  syncRail();
  syncZoomCeiling();
  // Nothing on screen and nothing on the map: the layers are never added,
  // so a viewer who asks for no station pays nothing for the products
  // existing.
  if (!drawSoundings && !drawAirports && stationLayers === null) return;
  const layers = ensureStationLayers();
  if (!layers) return;
  layers.setSoundings(drawSoundings ? soundingLoaded!.index : null);
  layers.setAirports(drawAirports ? airportLoaded!.index : null);
  syncStationTime();
  closeStationCard();
}

/** The valid time the marks are dimmed against: the playhead's, or now
 * before a run has loaded. */
function syncStationTime(): void {
  if (!stationLayers) return;
  const index = activeFrameIndex ?? requestedFrameIndex;
  stationLayers.setTime(metadata && index !== null ? frameValidTime(index) : Date.now());
}

function stationPointAt(point: { x: number; y: number }): { data: StationPointData; lngLat: [number, number] } | null {
  if (!stationLayers) return null;
  const layers = STATION_CLICKABLE_LAYERS.filter((id) => map.getLayer(id) !== undefined);
  if (!layers.length) return null;
  const box: [[number, number], [number, number]] = [
    [point.x - 6, point.y - 6],
    [point.x + 6, point.y + 6],
  ];
  for (const feature of map.queryRenderedFeatures(box, { layers })) {
    const data = stationDataOf(feature);
    if (data) return { data, lngLat: [data.station.lon, data.station.lat] };
  }
  return null;
}

function showStationCard(data: StationPointData, lngLat: [number, number]): void {
  closeStationCard();
  const options = {
    formatTime: (time: string) => formatDate(time),
    darkGround: document.body.dataset.ground === "dark",
    // The card's button pins the probe on the station itself, which is
    // where the whole ascent, or the airport's day of reports, is read;
    // a sounding's card also opens the chart, since that is what was asked.
    onPin: () => {
      closeStationCard();
      setProbe(data.station.lon, data.station.lat);
      if (data.kind === "sounding") soundingSection.open();
    },
  };
  const content =
    data.kind === "sounding" ? buildSoundingCard(data.station, options) : buildAirportCard(data.station, options);
  stationCard = new Popup({ closeButton: false, closeOnClick: false, className: "station-popup", maxWidth: "280px", offset: 10 })
    .setLngLat(lngLat)
    .setDOMContent(content)
    .addTo(map);
}

function closeStationCard(): void {
  stationCard?.remove();
  stationCard = null;
}

/** Each tile is its own switch: pressing it draws that product, pressing
 * it again takes it off, and the address bar carries what is on. */
function toggleStationProduct(product: keyof StationsUrlState): void {
  view.marks.stations = { ...view.marks.stations, [product]: !view.marks.stations[product] };
  syncUrl();
  applyStationView();
}

soundingTile.addEventListener("click", () => toggleStationProduct("soundings"));
airportTile.addEventListener("click", () => toggleStationProduct("airports"));

function ensureLayers(): void {
  if (layersAdded) return;
  map.addLayer(slots.fill.layer, FORECAST_ANCHOR_LAYER);
  // The derived fields sit over the fill and under the lines: a tint over
  // the rain, the isobars over both.
  if (composite) {
    for (const derived of Object.values(composite.layers)) {
      derived.layer.setVisible(false);
      map.addLayer(derived.layer, FORECAST_ANCHOR_LAYER);
    }
  }
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
    layer.setPalette(
      experimentEnabled && session.chartId === "prate"
        ? steppedPrecipitationPalette(session.variable)
        : buildPalette(session.variable, session.identity),
    );
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
 * field's own ceiling, the vapour flux ramp, or — for the wave vector,
 * whose magnitude is the significant wave height — the wave height ramp. */
function vectorPalette(session: VariableSession): Uint8Array {
  const max = sessionMaxMagnitude(session);
  const family = session.identity?.family;
  if (family === "qflux") return buildVapourFluxPalette(max);
  if (family === "wave") return buildWaveFieldPalette(max);
  return buildWindFieldPalette(max);
}

/** How fast the particles run over a vector session's field, relative to
 * the wind's pace, where the field is a speed: the wave vector's magnitude
 * is a height, and a metre of sea is made to travel like three metres a
 * second of wind — a three-metre sea like a fresh breeze, a storm sea past
 * the ramp's ceiling like a gale. */
function sessionParticlePace(session: VariableSession): number {
  return session.identity?.family === "wave" ? WAVE_PARTICLE_PACE : 1;
}
const WAVE_PARTICLE_PACE = 3;

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
  if (view.particles === next) return;
  view.particles = next;
  particlesChosen = true;
  syncRail();
  try {
    localStorage.setItem(PARTICLES_KEY, next ? "1" : "0");
  } catch {
    // The URL below still carries the choice for this session.
  }
  syncUrl();
  const session = activeSession;
  if (!session) return;
  if (!session.vector) {
    // Over a scalar fill the particles are the experiment's flow, when it
    // has one: a visibility flip, and the frame's planes handed over again.
    if (!composite?.flow) return;
    if (next) {
      ensureWindLayer();
      ensureWindGrid(composite.flow);
    }
    windLayer?.setVisible(next);
    if (next && activeFrameIndex !== null) trySelectComposite(activeFrameIndex);
    return;
  }
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
    target.setDomain(modelDomain());
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
  // An observation window is named by its newest frame, not by where it
  // starts: that is what a viewer of a live feed wants to know. Stamped in
  // UTC like a run cycle.
  if (showingObservations()) sayText(runTime, formatStamp(frameValidTime(frameCount() - 1), "UTC"));
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
  // Both ends of the track name a lead time, not the clock: the first frame
  // is the analysis on most axes, the first real step on a series that
  // skips it (ECMWF prate, the gust), and the window's start on an
  // observation.
  const offsets = frameOffsets(time);
  trackStart.textContent = formatTrackEnd(offsets[0]! * axisUnitSeconds(time));
  trackHorizon.textContent = formatTrackEnd(offsets.at(-1)! * axisUnitSeconds(time));
  buildTicks(time.frameCount);
  buildForecastDays();
  dataCardIndex.textContent = `${time.frameCount}F`;
  buildPreloadSegments(time.frameCount);
}

/** An end label of the track: a lead time in whole hours, `+0H` to `+240H`. */
function formatTrackEnd(leadSeconds: number): string {
  return `+${Math.round(leadSeconds / HOUR_SECONDS)}H`;
}

/** Let the camera go as deep as the primary's grid is worth — the ceiling
 * is the data's, so a 0.02° mosaic opens two zoom levels the 0.25° models
 * never had. MapLibre clamps the camera at once when the ceiling drops
 * below it, which is what a switch back to a coarser dataset wants. */
function applyZoomCeiling(session: VariableSession): void {
  const grid = geoGrid(session.metadata);
  dataZoomCeiling = zoomCeilingForStep(grid.longitudeStep, ZOOM_CEILING_CELL_PIXELS, BASE_MAX_ZOOM);
  syncZoomCeiling();
}

/** The data's ceiling, or the station marks' when either product is asked
 * for — whichever lets the camera deeper. Asked for, not loaded: the run
 * usually lands before the indexes do, and a ceiling that followed the
 * indexes would first pull a deep link at zoom 9 back to 7 and only then
 * let go. A product that never loads leaves a ceiling nothing needs, which
 * costs nothing. */
function syncZoomCeiling(): void {
  const marks = view.marks.stations.soundings || view.marks.stations.airports;
  const ceiling = Math.max(dataZoomCeiling, marks && activeCase === null ? STATION_MAX_ZOOM : 0);
  if (map.getMaxZoom() === ceiling) return;
  map.setMaxZoom(ceiling);
  // The navigation control greys its + only on zoom events, so a ceiling
  // lifted while the camera sits at the old one would leave the button
  // dead until the next pinch; a zoom event with nothing moved refreshes it.
  map.fire("zoom");
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
  setComposition(compositionForPrimary(session.id, view.lines));
  applyZoomCeiling(session);
  syncTimeline(session);
  const slot = slotFor(session.id);
  configureSlotLayer(slot, session, false);
  // The fill slot is only ever primary: with a surface as the view it goes
  // dark. The lines slot is reconciled against the composition below.
  if (slot !== slots.fill) detachSlot(slots.fill);
  const wind = session.vector;
  if (wind && view.particles) {
    ensureWindLayer();
    ensureWindGrid(session);
    // Back from the experiment's flow, if it was on: the wind's own ink.
    windLayer?.setInk(particleInk());
  }
  windLayer?.setVisible(wind && view.particles);
  applyOverlays();
  applyComposite();
  updateVariablePresentation(session);
  syncUrl();
  updateCacheReadout();
  // The data card reflects only the variable on screen: its own delivery
  // format, its own downloaded bytes, and its own delivery state — never a
  // cross-variable total.
  preloadFormat.value = formatReadout(session.format);
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
    requestAllProbeSeries();
    ensureProbeSessions();
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
  const wanted = view.field !== null ? view.lines : null;
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
      if (view.field === null || view.lines !== wanted) return;
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

/** The experiment's key under the colour scale: a row per derived layer
 * the run can feed — the frontal zone needs the θe, the inflow both
 * inputs — and nothing at all outside the experiment. */
function syncDerivedLegend(): void {
  const warmth = experimentEnabled && manifest !== null && hasBundle(manifest, EXPERIMENT_WARMTH_ID);
  const flow = warmth && compositeFlowPublished();
  legendInflow.hidden = !(flow && view.derived.inflow);
  legendFront.hidden = !(warmth && view.derived.front);
  legendDerived.hidden = legendInflow.hidden && legendFront.hidden;
}

/** The rail's derived-layer tiles: shown under the experiment on a run that
 * ships their inputs, pressed while the layer is drawn. */
function syncDerivedTiles(run: ForecastManifest): void {
  const warmth = experimentEnabled && hasBundle(run, EXPERIMENT_WARMTH_ID);
  const flow = warmth && hasBundle(run, EXPERIMENT_FLOW_ID);
  for (const button of derivedButtons()) {
    const id = button.dataset.derived as "inflow" | "front";
    button.hidden = id === "inflow" ? !flow : !warmth;
  }
  syncRail();
}

/** Flip one derived layer: off hides it at once, on shows it from the
 * frame on screen (computing it if its inputs are decoded). */
function setDerivedShown(id: "inflow" | "front", shown: boolean): void {
  if (view.derived[id] === shown) return;
  view.derived[id] = shown;
  syncRail();
  syncDerivedLegend();
  syncUrl();
  if (!composite) return;
  if (!shown) composite.layers[id].layer.setVisible(false);
  else if (activeFrameIndex !== null) trySelectComposite(activeFrameIndex);
}

/** Whether the run on screen ships the experiment's flow bundle — the
 * particle switch shows over a scalar fill when it does. */
function compositeFlowPublished(): boolean {
  return composite !== null && manifest !== null && hasBundle(manifest, EXPERIMENT_FLOW_ID);
}

/** Open the experiment's input sessions beside the composition — after the
 * primary, never blocking it, at the overlay tier — and hand each the frame
 * on screen as it lands. A run that does not ship an input leaves that
 * field off: the frontal zone needs the θe alone, the inflow both. */
function applyComposite(): void {
  if (!composite || !manifest) return;
  const sequence = initializeSequence;
  const inputs: Array<["flow" | "warmth", ForecastBundleId]> = [
    ["flow", EXPERIMENT_FLOW_ID],
    ["warmth", EXPERIMENT_WARMTH_ID],
  ];
  for (const [role, id] of inputs) {
    if (composite[role]?.id === id || !hasBundle(manifest, id)) continue;
    loadVariable(id, sequence, "overlay")
      .then((session) => {
        if (sequence !== initializeSequence || !composite) return;
        composite[role] = session;
        refreshViewportTiles();
        const index = activeFrameIndex ?? requestedFrameIndex ?? Number(slider.value);
        sendPrefetchWindow(index);
        if (activeFrameIndex !== null) trySelectComposite(activeFrameIndex);
        else requestCompositeDecode(session, index);
        if (activeSession) {
          particlesToggle.hidden = !activeSession.vector && !compositeFlowPublished();
          syncRailDensity();
        }
      })
      .catch((error: unknown) => {
        if (sequence !== initializeSequence) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        console.warn(`composite input ${id} unavailable`, error);
      });
  }
}

/** Forget the experiment's sessions and blank its layers: a new dataset. */
function resetComposite(): void {
  if (!composite) return;
  composite.flow = null;
  composite.warmth = null;
  composite.wantedKeys.clear();
  composite.shownKeys = [];
  for (const derived of Object.values(composite.layers)) {
    derived.layer.setVisible(false);
    derived.gridSource = null;
    derived.builtFrom = null;
  }
}

/** Ask an input session for its whole planes at a primary frame. */
function requestCompositeDecode(session: VariableSession, index: number): void {
  const offset = sessionOffsetForLead(session, frameLeadSeconds(index));
  if (offset === null) return;
  for (const variable of session.variables) {
    const key = cacheKey(session, variable, offset);
    const frame = planeCache.get(key);
    if (!frame || frame.tiles !== null) requestDecode(session, variable, offset);
  }
}

/** An input session's whole planes for a lead time, straight out of the
 * cache, or null when any is missing — those are then asked for and noted,
 * so their arrival retries the composite. A lead the session's axis lacks is
 * null too, and nothing is asked. */
function compositeInputPlanes(session: VariableSession, lead: number): { keys: string[]; frames: DecodedFrame[] } | null {
  if (!composite) return null;
  const offset = sessionOffsetForLead(session, lead);
  if (offset === null) return null;
  const keys = session.variables.map((variable) => cacheKey(session, variable, offset));
  const frames: DecodedFrame[] = [];
  let complete = true;
  for (const [position, key] of keys.entries()) {
    const frame = planeCache.get(key);
    if (frame && frame.tiles === null) {
      frames.push(frame);
      continue;
    }
    complete = false;
    composite.wantedKeys.add(key);
    requestDecode(session, session.variables[position]!, offset);
  }
  if (!complete) return null;
  for (const [position, key] of keys.entries()) {
    planeCache.delete(key);
    planeCache.set(key, frames[position]!);
  }
  return { keys, frames };
}

/** Put a derived plane on its layer, configured for the grid it was
 * computed on, unless the plane on screen was built from these very
 * inputs already. */
function showDerived(derived: DerivedLayer, source: VariableSession, builtFrom: string, compute: () => Uint8Array): void {
  if (derived.builtFrom === builtFrom) {
    derived.layer.setVisible(true);
    return;
  }
  if (derived.gridSource !== source.metadata) {
    derived.layer.configureGrid(source.metadata);
    derived.layer.setDomain(modelDomain());
    derived.layer.setContours(derivedContourStyle(derived.id));
    derived.layer.setVectorField(null);
    derived.layer.setPalette(derived.id === "inflow" ? inflowPalette() : frontPalette());
    derived.gridSource = source.metadata;
  }
  derived.layer.setFrame(compute());
  derived.layer.setVisible(true);
  derived.builtFrom = builtFrom;
}

/** A derived layer draws as an outlined region, not a coat: the palette
 * fill is turned down to a wash so the rain underneath still reads, and
 * the shader's named contours trace the index at two strengths in the
 * layer's own ink — where the inflow and the rain band overlap, both are
 * seen, the way a chart's translucent arrow sits over its shading.
 *
 * The plane's codes are the index itself (0..DERIVED_MAX_CODE), decoded
 * here as 50 + index so the regular contour family, which the shader
 * always draws at multiples of the interval, never lands: an interval of
 * 100 has no multiple between 50 and 51, and the zero contour that would
 * otherwise ring every faint patch is out of range. */
function derivedContourStyle(id: DerivedLayer["id"]): ContourStyle {
  const inflow = id === "inflow";
  return {
    offset: DERIVED_DECODE_OFFSET,
    scale: 1 / DERIVED_MAX_CODE,
    interval: 100,
    emphasisInterval: 0,
    values: (inflow ? [0.3, 0.65] : [0.4]).map((index) => DERIVED_DECODE_OFFSET + index),
    lineWidth: 0.9,
    emphasisWidth: 0.9,
    lineColor: inflow ? [0.8, 0.27, 0.15, 0.95] : [0.45, 0.17, 0.59, 0.95],
    fillAlpha: inflow ? 0.4 : 0.45,
    smoothing: 0,
  };
}
const DERIVED_DECODE_OFFSET = 50;

/** Compute and show the experiment's fields for the primary frame at
 * `index`, and run the particles off the flow, from whatever input planes
 * are decoded: a field whose inputs are not all there yet keeps its last
 * plane up, the way an overlay does, and a field whose input the run does
 * not ship stays off. The fill's own vector, when the fill is one, keeps
 * the particles; the flow only drives them over a scalar. */
function trySelectComposite(index: number): void {
  if (!composite || !layersAdded) return;
  const lead = frameLeadSeconds(index);
  const flow = composite.flow ? compositeInputPlanes(composite.flow, lead) : null;
  const warmth = composite.warmth ? compositeInputPlanes(composite.warmth, lead) : null;
  composite.shownKeys = [...(flow?.keys ?? []), ...(warmth?.keys ?? [])];
  for (const key of composite.shownKeys) composite.wantedKeys.delete(key);
  if (warmth && composite.warmth && view.derived.front) {
    const session = composite.warmth;
    const plane = warmth.frames[0]!.plane;
    showDerived(composite.layers.front, session, warmth.keys.join("|"), () =>
      thermalFrontZone({ grid: geoGrid(session.metadata), variable: session.variable, plane }),
    );
  }
  if (flow && warmth && composite.flow && composite.warmth && view.derived.inflow) {
    const flowSession = composite.flow;
    const warmthSession = composite.warmth;
    const [u, v] = flowSession.variables;
    if (u && v && flow.frames.length >= 2) {
      showDerived(composite.layers.inflow, flowSession, [...flow.keys, ...warmth.keys].join("|"), () =>
        warmMoistInflow(
          { grid: geoGrid(flowSession.metadata), u, v, uPlane: flow.frames[0]!.plane, vPlane: flow.frames[1]!.plane },
          { grid: geoGrid(warmthSession.metadata), variable: warmthSession.variable, plane: warmth.frames[0]!.plane },
        ),
      );
    }
  }
  if (flow && composite.flow && flow.frames.length >= 2 && view.particles && activeSession && !activeSession.vector) {
    ensureWindLayer();
    ensureWindGrid(composite.flow);
    windLayer!.setInk(experimentFlowInk());
    windLayer!.setWindPlanes(flow.frames[0]!.plane, flow.frames[1]!.plane);
    windLayer!.setVisible(true);
  }
}

/** Switch to a composition: load its primary if it is not the one on screen
 * (the existing variable switch), then reconcile the overlays. A slot the
 * run does not ship empties rather than errors. */
async function activateComposition(next: ViewComposition): Promise<void> {
  variableChosen = true;
  if (!manifest || !layersAdded || switchingVariable) return;
  const resolved = resolveComposition(next, manifest, activeCase?.defaultVariable ?? DEFAULT_VARIABLE);
  const primary = compositionPrimary(resolved, DEFAULT_VARIABLE);
  setComposition(resolved);
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
  variableChosen = true;
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

/** Tune to the dataset the URL names and open its first layer.
 *
 * `frame` says whether the camera goes to the dataset's own region — a
 * case's box, a regional model's footprint. A switch frames; the first
 * open frames unless the link fixed the view; a retry or a new run of the
 * same model never moves a camera the viewer has since placed. */
async function initialize({ frame = false }: { frame?: boolean } = {}): Promise<void> {
  const sequence = ++initializeSequence;
  // Taken here, so a resume the poll asked for never outlives the open it
  // was meant for.
  const resume = resumeOnNewRun;
  resumeOnNewRun = null;
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
  currentManifestCrc = null;
  metadata = null;
  for (const slot of [slots.fill, slots.lines]) {
    detachSlot(slot);
    slot.gridSource = null;
  }
  windLayerGridSource = null;
  windLayer?.setVisible(false);
  resetComposite();
  particlesToggle.hidden = true;
  syncRailDensity();
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
      if (!caseDefaultApplied && !requestedView.fieldRequested) {
        setComposition(compositionForPrimary(found.defaultVariable, view.lines));
      }
      caseDefaultApplied = true;
      document.body.classList.add("is-showcase");
      updateCasePresentation(found);
      // Frame the event before any byte lands, so the first painted plane
      // arrives on the region it belongs to rather than on the world view.
      applyCaseCamera(found, frame);
      const loadedCase = await fetchCaseManifest(dataBaseUrl(), found);
      if (sequence !== initializeSequence) return;
      loadedManifest = loadedCase.manifest;
      manifestUrl = loadedCase.manifestUrl;
      currentRun = found.run;
    } else {
      // A regional model opened on a view showing little of it would paint
      // nothing worth the name: frame its region first, the way a case is
      // framed, but without holding the camera there.
      if (frame) frameModelRegion();
      const loaded = await fetchManifest(dataBaseUrl(), selectedModelId);
      if (sequence !== initializeSequence) return;
      loadedManifest = loaded.manifest;
      manifestUrl = loaded.manifestUrl;
      currentRun = loaded.latest.run;
      currentManifestCrc = loaded.latest.manifestCrc32;
    }
    manifest = loadedManifest;
    // The dataset is settled here (a case pins its own), so the timeline can
    // be titled for what it actually shows.
    applyDatasetWording();
    // A case is a fixed past; the live storms and stations have no place
    // over it.
    void applyTcView();
    applyStationView();
    // The cycle is named in UTC wherever it is stamped (00Z is its name),
    // whatever zone the valid times below read in. An observation window's
    // runTime is only where it starts; its line is stamped with the newest
    // frame once the session's axis is known (syncTimeline).
    if (!showingObservations()) sayText(runTime, formatStamp(loadedManifest.runTime, "UTC"));

    // A slot this run does not ship empties; a case names its own default
    // for when that leaves nothing, and a live run always carries its core
    // set — the forecast pair, or the reflectivity on a radar mosaic, which
    // is what a switch onto one opens.
    if (!activeCase && !variableChosen && !experimentEnabled) {
      setComposition(compositionForPrimary(modelDefaultVariable(selectedModelId, DEFAULT_VARIABLE), view.lines));
    }
    setComposition(
      resolveComposition(
        viewComposition(),
        loadedManifest,
        activeCase?.defaultVariable ?? FORECAST_MODELS[selectedModelId].coreBundles?.[0] ?? DEFAULT_VARIABLE,
      ),
    );
    selectedVariableId = compositionPrimary(viewComposition(), DEFAULT_VARIABLE);
    // The rail for this run: the field section from what it ships and what
    // is about to be on screen (a showcase case ships only the fields its
    // event is about, so even the core can be missing), the pressure switch
    // when it has a surface to chart, the experiment's tiles on its inputs.
    for (const button of variableButtons()) {
      if (button.dataset.group !== "pressure") continue;
      button.hidden = !PRESSURE_BUNDLE_IDS.some((id) => hasBundle(loadedManifest, id));
    }
    syncFieldTiles(loadedManifest);
    syncDerivedTiles(loadedManifest);

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
    if (resume) {
      // The same dataset, a newer window: back to the observation time the
      // viewer was on, or to the new end for one who was at the old end.
      const index = resume.atEnd ? frameCount() - 1 : nearestFrameIndexForValidTime(resume.validTime);
      generation += 1;
      trySelectFrame(index);
    }

    say(loadStatus, "framesReady", { count: frameCount() });
    loadStatus.className = "load-status";
    slider.disabled = false;
    playButton.disabled = false;
    speedButton.disabled = false;
    setVariableButtonsDisabled(false);
    if (!reducedMotion.matches && (resume === null || resume.playing)) startPlayback();
  } catch (error) {
    if (sequence !== initializeSequence) return;
    if (error instanceof DOMException && error.name === "AbortError") return;
    if (error instanceof ManifestRejectedError && reloadForNewerShell(error.manifestCrc32)) return;
    if (error instanceof StoreRejectedError && currentManifestCrc !== null && reloadForNewerShell(currentManifestCrc)) return;
    showError(error instanceof Error ? error.message : t("bundleLoadFailed"));
  }
}

/** A live run's store this shell could not open (`loadVariable`): carried
 * to `initialize`'s recovery as its own kind so a tab older than the data
 * reloads once, the way it does for a manifest it refuses. */
class StoreRejectedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoreRejectedError";
  }
}

/** The one cure for a tab older than the data: a live manifest this shell
 * refuses is, far more often than not, one published in a schema a newer
 * shell reads — a run that ships only its Zarr stores reaching a tab that
 * predates the store — and the pointer poll would otherwise leave the tab
 * on an error until someone refreshes it. Reload once per manifest: the
 * crc32 is remembered for the tab's life, so a manifest the new shell
 * refuses too shows its error instead of looping. */
const RELOADED_FOR_KEY = "xue-reloaded-for-manifest";
function reloadForNewerShell(manifestCrc32: string): boolean {
  try {
    if (sessionStorage.getItem(RELOADED_FOR_KEY) === manifestCrc32) return false;
    sessionStorage.setItem(RELOADED_FOR_KEY, manifestCrc32);
  } catch {
    return false;
  }
  window.location.reload();
  return true;
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
      // The pressure tile is the lines' switch: on over the field, off
      // again; from the lines-alone view it puts the field back rather
      // than leaving the screen empty.
      if (view.lines === null) void activateComposition({ fill: view.field, lines: preferredPressureVariable() });
      else if (view.field === null) restoreField();
      else void activateComposition({ fill: view.field, lines: null });
    } else if (family !== undefined) {
      // A family tile opens the member last on screen; the level row then
      // moves between its surfaces. Pressed already, a family of variants
      // (sea ice, the waves) cycles to the next one the run publishes — a
      // shortcut beside the sheet's chips, read back off the tile's gloss.
      // Lines over it stay.
      const run = manifest;
      const variants = run ? familyVariants(family, (member) => hasBundle(run, member)).filter((member) => hasBundle(run, member)) : [];
      const at = view.field === null ? -1 : variants.indexOf(view.field);
      const next = at >= 0 && variants.length > 1 ? variants[(at + 1) % variants.length]! : preferredFamilyMember(family);
      void activateComposition({ fill: next, lines: view.lines });
    } else {
      // A fill tile changes the field alone; lines over it stay.
      void activateComposition({ fill: id, lines: view.lines });
    }
  });
}
for (const button of derivedButtons()) {
  button.addEventListener("click", () => {
    const id = button.dataset.derived as "inflow" | "front";
    setDerivedShown(id, !view.derived[id]);
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
    void initialize({ frame: true });
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
  windLayer?.setInk(activeSession && !activeSession.vector && composite?.flow ? experimentFlowInk() : particleInk());
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
  renderLanguageList();
  probePanel.root.setAttribute("aria-label", t("probeAria"));
  probePanel.close.setAttribute("aria-label", t("probeCloseAria"));
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
  renderTcSheet();
}

/** Rewrite every valid-time stamp in the display zone in force: the
 * capsule's readout and tooltip, the day marks along the track, and the
 * meteogram's (which the probe redraws). The run cycle is not one. */
function applyDisplayZone(): void {
  if (!metadata) return;
  updateFrameReadout(activeFrameIndex ?? Number(slider.value));
  buildForecastDays();
}

// Neither the locale nor the theme reloads: the picker and the toggle each
// persist the choice and repaint in place through the two listeners here.
// The language picker is wired where it is built, beside the other sheets.
onThemeChange(applyAppearance);
onLocaleChange(applyLocale);
// The display zone moves with the pin; every valid-time stamp follows it.
onDisplayZoneChange(applyDisplayZone);
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
  // A storm point under the pointer opens its card; the probe pin is for
  // the field.
  const hit = tcPointAt(event.point);
  if (hit) {
    closeStationCard();
    showTcCard(hit.data, hit.lngLat);
    return;
  }
  closeTcCard();
  // A station mark under the pointer opens its card instead of pinning the
  // field: the storm marks take precedence, the stations come next, and the
  // probe is what a click on the field itself does.
  const station = stationPointAt(event.point);
  if (station) {
    showStationCard(station.data, station.lngLat);
    return;
  }
  closeStationCard();
  setProbe(event.lngLat.lng, event.lngLat.lat);
});
map.on("mousemove", (event) => {
  if (!tcLayers && !stationLayers) return;
  const station = stationPointAt(event.point);
  // The mark under the pointer grows by a pixel, which is the only feature
  // state either product keeps.
  stationLayers?.setHover(station?.data ?? null);
  map.getCanvas().style.cursor = tcPointAt(event.point) || station ? "pointer" : "";
});
window.addEventListener("pointerdown", (event) => {
  if (!contextMenu.hidden && !contextMenu.contains(event.target as Node)) hideContextMenu();
});
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  // The sheets close themselves on Escape (createSheet wires that); this
  // handler carries the surfaces that have no sheet of their own.
  hideContextMenu();
  if (!skewtSheetTookEscape) closeProbe();
  closeTcCard();
  closeStationCard();
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
particlesToggle.addEventListener("click", () => setParticlesEnabled(!view.particles));
/** Poll the live pointer; a changed manifest re-initializes onto the new
 * run ("排播型电视直播" — the client tunes itself to the newest broadcast).
 * The manifest's crc is what says "new": a forecast's changes with its run
 * id, an observation window's every few minutes under the same id. */
async function checkForNewRun(): Promise<void> {
  // A case is a fixed historical run; there is no newer one to move to.
  if (activeCase || !currentRun || currentManifestCrc === null || document.hidden || switchingVariable) return;
  try {
    const model = selectedModelId;
    const latest = await fetchLatestPointer(dataBaseUrl(), model);
    if (model !== selectedModelId || activeCase || currentManifestCrc === null) return;
    if (latest.manifestCrc32 === currentManifestCrc) return;
    // A rolling window moves under the viewer: the playhead keeps its
    // place by observation time, and one left at the newest frame follows
    // the window's end — the live default. The same run topped up with a
    // bundle keeps its place too (the axis is the same). A new forecast
    // cycle opens at its analysis as it always has.
    if ((showingObservations() || latest.run === currentRun) && metadata) {
      const index = activeFrameIndex ?? Number(slider.value);
      resumeOnNewRun = {
        validTime: frameValidTime(index),
        atEnd: index >= frameCount() - 1,
        playing,
      };
    }
    void initialize();
  } catch {
    // Transient poll failures never disturb the running app.
  }
}
/** Where the playhead goes once a new run of the same dataset has opened
 * from the pointer poll; null for every other open. */
let resumeOnNewRun: { validTime: number; atEnd: boolean; playing: boolean } | null = null;
/** The pointer poll, rescheduled after each check so the interval follows
 * the dataset on screen. */
function schedulePointerPoll(): void {
  window.setTimeout(() => {
    void checkForNewRun().finally(schedulePointerPoll);
  }, latestPollMs());
}
schedulePointerPoll();
window.setInterval(() => void loadTc(), LATEST_POLL_MS);
window.setInterval(() => void loadStations(), LATEST_POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPlayback();
  else {
    void checkForNewRun();
    void loadTc();
    void loadStations();
  }
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
syncRail();
buildTicks(FRAME_COUNT);
resetPreloadCard(FRAME_COUNT);
map.once("load", () => {
  mapStyleReady = true;
  // A theme or language picked while the style was still loading was built
  // into nothing; the sync is a no-op otherwise, and applies the ground.
  syncBasemapStyle();
  // A link that fixed the view is opened on that view; every other opens
  // on the dataset's own region.
  void initialize({ frame: urlCamera === null });
  void loadTc();
  void loadStations();
});
