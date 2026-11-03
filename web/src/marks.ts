// The marks beside the runs: storm tracks (tc/), station products
// (stations/, aq/), fire detections (fire/) and warning areas (alerts/).
// Those directories hold the layers, cards and fetches; this is the
// controller that loads, draws and follows the playhead.
import { type Map as MaplibreMap, Popup } from "maplibre-gl";
import { buildAlertsCard } from "./alerts/card";
import { fetchAlertLines, fetchAlertsIndex, type LoadedAlertsIndex } from "./alerts/fetch";
import { type AlertHit, AlertLayers } from "./alerts/layer";
import type { AlertLine } from "./alerts/schema";
import { buildAqCard } from "./aq/card";
import { fetchAqIndex, fetchAqStation, type LoadedAqIndex } from "./aq/fetch";
import { cardPopup } from "./cardpopup";
import type { CaseMarkerLayers } from "./casemarkers";
import { decodeTable, nearestCell } from "./composite";
import { buildFireCard } from "./fire/card";
import { fetchFireIndex, type LoadedFireIndex } from "./fire/fetch";
import { FIRE_CLICKABLE_LAYERS, type FireDetection, FireLayers } from "./fire/layer";
import { FireWindArrow, meanWind } from "./fire/windarrow";
import { t } from "./i18n";
import {
  axisUnitSeconds,
  type BundleMetadata,
  type BundleVariable,
  type ForecastManifest,
  type ForecastModelId,
  frameOffsets,
  hasBundle,
} from "./manifest";
import type { LongitudeBand } from "./mosaic";
import type { WindParticleLayer } from "./particles";
import { geoGrid } from "./probe";
import type { ProbePanel } from "./probepanel";
import type { RiverGauge } from "./river/index";
import type { VariableSession } from "./session";
import type { createSheet } from "./sheet";
import { fetchCaseManifest, fetchCatalog, type ShowcaseCase } from "./showcase-catalog";
import { dataBaseUrl } from "./site";
import type { SoundingSection } from "./sounding/section";
import { buildAirportCard, buildSoundingCard, buildSynopCard } from "./stations/card";
import {
  fetchAirportIndex,
  fetchSoundingIndex,
  fetchSynopIndex,
  type LoadedAirportIndex,
  type LoadedSoundingIndex,
  type LoadedSynopIndex,
} from "./stations/fetch";
import { STATION_CLICKABLE_LAYERS, stationDataOf, StationLayers, type StationPointData } from "./stations/layers";
import { TC_MODELS } from "./tc/agencies";
import { buildTcCard } from "./tc/card";
import { pointDataOf, StormLayers, type StormView, TC_CLICKABLE_LAYERS, type TcPointData } from "./tc/layers";
import { renderTcPanel } from "./tc/panel";
import type { TcIndexEntry, TcStorm } from "./tc/schema";
import { fetchTcIndex, fetchTcStorm, type LoadedTcIndex, resolveTcStormId, stormBounds } from "./tc/tracks";
import type { TileRect } from "./tiles";
import type { StationsUrlState } from "./urlstate";
import type { ViewState } from "./viewstate";

/** Mark layers go under the basemap's first symbol layer, so the place
 * labels stay readable over them. */
export function firstSymbolLayerId(map: MaplibreMap): string | undefined {
  return (map.getStyle().layers ?? []).find((entry) => entry.type === "symbol")?.id;
}

/** A case's borrowed wind run, opened like a mosaic member that never
 * takes a slot of its own. */
interface BorrowedRun {
  id: ForecastModelId;
  manifest: ForecastManifest;
  manifestUrl: string;
  run: string;
  manifestCrc: string;
  band: LongitudeBand;
  slot: null;
  wanted: null;
}

/** What the marks read off the page: the map, the rail tiles they own the
 * state of, the run and the playhead (read live, through getters), and the
 * few page functions a mark's click or a wind series reaches back into. */
export interface MarksDeps {
  readonly map: MaplibreMap;
  readonly particlesToggle: HTMLButtonElement;
  readonly levelRow: HTMLElement;
  readonly tcTile: HTMLButtonElement;
  readonly soundingTile: HTMLButtonElement;
  readonly airportTile: HTMLButtonElement;
  readonly synopTile: HTMLButtonElement;
  readonly fireTile: HTMLButtonElement;
  readonly aqTile: HTMLButtonElement;
  readonly alertsTile: HTMLButtonElement;
  readonly tcList: HTMLElement;
  readonly tcLevelGroup: { root: HTMLDivElement; chip: HTMLButtonElement; };
  readonly tcSheetControl: ReturnType<typeof createSheet>;
  readonly memberOffsetForFrame: (session: VariableSession, index: number) => number | null;
  readonly manifest: ForecastManifest | null;
  readonly metadata: BundleMetadata | null;
  readonly layersAdded: boolean;
  readonly windLayer: WindParticleLayer | null;
  readonly requestedCaseId: string | null;
  readonly activeCase: ShowcaseCase | null;
  readonly activeSession: VariableSession | null;
  readonly activeFrameIndex: number | null;
  readonly requestedFrameIndex: number | null;
  readonly initializeSequence: number;
  readonly generation: number;
  readonly ready: boolean;
  readonly planeCache: ReadonlyMap<string, { plane: Uint8Array; tiles: TileRect[] | null }>;
  nextRequestId: number;
  readonly view: ViewState;
  readonly refreshParticleInk: () => void;
  readonly formatDate: (value: string | number) => string;
  readonly frameValidTime: (index: number) => number;
  readonly cacheKey: (session: VariableSession, variable: BundleVariable, offset: number) => string;
  readonly probePanelControl: Pick<ProbePanel, "probe">;
  readonly resolveProbeAlerts: () => void;
  readonly setProbe: (longitude: number, latitude: number, gauge?: RiverGauge | null) => void;
  readonly soundingSection: SoundingSection;
  readonly ensureWindGrid: (session: VariableSession) => void;
  readonly requestDecode: (session: VariableSession, variable: BundleVariable, hour: number) => void;
  readonly syncRailDensity: () => void;
  readonly syncRail: () => void;
  readonly syncCaseCaption: () => void;
  readonly caseMarkers: CaseMarkerLayers | null;
  readonly syncUrl: () => void;
  readonly loadVariable: (variableId: "wind10m", sequence: number, role: "overlay", member: BorrowedRun | null) => Promise<VariableSession>;
  readonly mapStyleReady: boolean;
  readonly loadLiveRadar: () => void;
  readonly caseFireLoaded: LoadedFireIndex | null;
  readonly syncRadarTime: () => void;
  readonly ensureWindLayer: () => WindParticleLayer;
}

/** The marks controller: the storms, the station products (soundings,
 * airports, synop, air quality), the fire detections and their case wind,
 * and the warning areas. Each polls or loads its index, draws its layers
 * under the basemap's labels and follows the playhead's valid time; none
 * takes a slot or gates the playhead. */
export function createMarks(deps: MarksDeps) {
  const {
    map,
    particlesToggle,
    levelRow,
    tcTile,
    soundingTile,
    airportTile,
    synopTile,
    fireTile,
    aqTile,
    alertsTile,
    tcList,
    tcLevelGroup,
    tcSheetControl,
    memberOffsetForFrame,
    planeCache,
    view,
    refreshParticleInk,
    formatDate,
    frameValidTime,
    cacheKey,
    probePanelControl,
    resolveProbeAlerts,
    setProbe,
    soundingSection,
    ensureWindGrid,
    requestDecode,
    syncRailDensity,
    syncRail,
    syncCaseCaption,
    syncUrl,
    loadVariable,
    loadLiveRadar,
    syncRadarTime,
    ensureWindLayer,
  } = deps;
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
    tcCard = cardPopup({ className: "tc-popup", maxWidth: "300px", offset: 12 })
      .setLngLat(lngLat)
      .setDOMContent(buildTcCard(data, { formatTime: (time) => formatDate(time) }))
      .addTo(map);
  }

  function closeTcCard(): void {
    tcCard?.remove();
    tcCard = null;
  }

  function ensureTcLayers(): StormLayers | null {
    if (!deps.mapStyleReady) return null;
    if (!tcLayers) tcLayers = new StormLayers(map);
    const before = firstSymbolLayerId(map);
    tcLayers.ensure(before);
    tcLayers.setInk(document.body.dataset.ground === "dark");
    return tcLayers;
  }

  /** The index entries the view draws: the focused storm, else every named
   * and every tracked-but-unnumbered system, plus the model-only ones when
   * asked for. */
  function tcEntriesToDraw(): TcIndexEntry[] {
    if (!tcLoaded || view.marks.tc.off || deps.activeCase !== null) return [];
    const storms = tcLoaded.index.storms;
    if (view.marks.tc.storm !== null) return storms.filter((entry) => entry.id === view.marks.tc.storm);
    return storms.filter((entry) => entry.level !== "C" || tcPotential);
  }

  /** The valid time the marks are drawn at: the playhead's, or now before a
   * run has loaded. */
  function syncTcTime(): void {
    if (!tcLayers) return;
    const index = deps.activeFrameIndex ?? deps.requestedFrameIndex;
    tcLayers.setTime(deps.metadata && index !== null ? frameValidTime(index) : Date.now());
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
    const hidden = deps.activeCase !== null || !tcLoaded || tcLoaded.index.storms.length === 0;
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
    tcTile.setAttribute("aria-pressed", String(!hidden && !view.marks.tc.off));
    renderTcLevelGroup();
    renderTcSheet();
    if (view.marks.tc.storm !== null && tcFramed !== view.marks.tc.storm && tcDrawn.length === 1) {
      tcFramed = view.marks.tc.storm;
      const bounds = stormBounds(tcDrawn[0]!);
      if (bounds) map.fitBounds(bounds, { padding: 80, maxZoom: 6, duration: 900 });
    }
  }

  /** The storm chip on the level row while tracks are drawn, after the
   * run's own groups; gone, with the sheet closed, while they are off. Keyed
   * on the index and the switch, not on the storms drawn, so it holds still
   * while their tracks load. */
  function renderTcLevelGroup(): void {
    const shown = deps.activeCase === null && !view.marks.tc.off && (tcLoaded?.index.storms.length ?? 0) > 0;
    if (!shown) {
      if (tcSheetControl.isOpen()) tcSheetControl.close();
      if (tcLevelGroup.root.isConnected) {
        tcLevelGroup.root.remove();
        levelRow.hidden = levelRow.childElementCount === 0;
      }
      return;
    }
    const focus = view.marks.tc.storm;
    const entry = focus === null ? null : tcLoaded?.index.storms.find((storm) => storm.id === focus);
    tcLevelGroup.chip.textContent = focus === null ? "All" : (entry?.name?.toUpperCase() ?? focus);
    tcLevelGroup.chip.setAttribute("aria-label", t("tcOptionsAria"));
    if (!tcLevelGroup.root.isConnected) levelRow.append(tcLevelGroup.root);
    levelRow.hidden = false;
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
      },
    );
  }

  // ---------------------------------------------------------------------------
  // Station marks: the point products beside the runs and the storms
  // (docs/sounding.md, docs/airport.md, docs/synop.md), drawn as MapLibre circle layers
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
  let synopLoaded: LoadedSynopIndex | null = null;
  /** The active fires (docs/fire.md) ride the station marks' poll, tile row
   * and `?stations=` switch; their layer is their own, clustered and read by
   * span rather than whole. */
  let fireLoaded: LoadedFireIndex | null = null;
  let fireLayers: FireLayers | null = null;
  /** The air-quality stations (docs/aq.md) are station marks of the same
   * layers; the weather warnings (docs/alerts.md) are areas, on a layer of
   * their own, and a snapshot of now that the playhead does not touch. */
  let aqLoaded: LoadedAqIndex | null = null;
  let alertsLoaded: LoadedAlertsIndex | null = null;
  let alertLayers: AlertLayers | null = null;
  /** The network files of the round on the map, read only while the
   * warnings are drawn: the index URL they belong to and each network's
   * lines (empty until read, or when the read failed). */
  let alertLines: { indexUrl: string; lines: AlertLine[][] } | null = null;
  /** What the warning layers were last given, so a poll that brought
   * nothing new rebuilds nothing. */
  let alertsDrawn: { indexUrl: string; lines: AlertLine[][] } | null = null;

  /** The wind a case's fire marks are read against (`fire/windarrow.ts`):
   * the case's own `wind10m`, or the one its `wind` block borrows from
   * another case — which then also runs the particles over the case's own
   * fields, on this case's clock. */
  const fireWind: {
    session: VariableSession | null;
    borrowed: boolean;
    /** The case the session was asked for, so a repeat asks nothing. */
    forCase: string | null;
    /** Decoded u, v series by cell of the session's grid. */
    series: Map<number, { u: Float32Array | null; v: Float32Array | null }>;
    /** Series requests in flight: the cell and the component each fills. */
    requests: Map<number, { cell: number; component: 0 | 1 }>;
    /** A borrowed wind's planes not decoded yet for the frame on screen. */
    wantedKeys: Set<string>;
    /** Its planes on screen, held from eviction. */
    shownKeys: string[];
    arrow: FireWindArrow | null;
    /** The marks' time: the playhead's, swept between frames on a case. */
    time: number | null;
  } = {
    session: null,
    borrowed: false,
    forCase: null,
    series: new Map(),
    requests: new Map(),
    wantedKeys: new Set(),
    shownKeys: [],
    arrow: null,
    time: null,
  };
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
    const [soundings, airports, synop, fire, aq, alerts] = await Promise.allSettled([
      fetchSoundingIndex(base),
      fetchAirportIndex(base),
      fetchSynopIndex(base),
      fetchFireIndex(base),
      fetchAqIndex(base),
      fetchAlertsIndex(base),
    ]);
    stationsPolled = true;
    // A poll that fails leaves the previous issue drawn; only an answer
    // replaces it.
    if (soundings.status === "fulfilled") soundingLoaded = soundings.value;
    if (airports.status === "fulfilled") airportLoaded = airports.value;
    if (synop.status === "fulfilled") synopLoaded = synop.value;
    if (fire.status === "fulfilled") fireLoaded = fire.value;
    if (aq.status === "fulfilled") aqLoaded = aq.value;
    if (alerts.status === "fulfilled") alertsLoaded = alerts.value;
    if (deps.activeCase === null && deps.requestedCaseId === null) loadLiveRadar();
    applyStationView();
  }

  function ensureStationLayers(): StationLayers | null {
    if (!deps.mapStyleReady) return null;
    if (!stationLayers) stationLayers = new StationLayers(map);
    const before = firstSymbolLayerId(map);
    stationLayers.ensure(before);
    stationLayers.setInk(document.body.dataset.ground === "dark");
    return stationLayers;
  }

  /** The tiles, the marks and the card, from what has loaded and what is
   * switched on. A case is a fixed past over one region; the live stations
   * have no place over it, so both tiles go away with it. */
  function applyStationView(): void {
    const hidden = deps.activeCase !== null;
    const soundingAvailable = soundingLoaded !== null && !hidden;
    const airportAvailable = airportLoaded !== null && !hidden;
    const synopAvailable = synopLoaded !== null && !hidden;
    // A case carries its own round, if any, in place of the live one.
    const fireAvailable = deps.activeCase !== null ? deps.activeCase.fire !== undefined && deps.caseFireLoaded !== null : fireLoaded !== null;
    let railChanged = false;
    if (soundingTile.hidden === soundingAvailable) {
      soundingTile.hidden = !soundingAvailable;
      railChanged = true;
    }
    if (airportTile.hidden === airportAvailable) {
      airportTile.hidden = !airportAvailable;
      railChanged = true;
    }
    if (synopTile.hidden === synopAvailable) {
      synopTile.hidden = !synopAvailable;
      railChanged = true;
    }
    if (fireTile.hidden === fireAvailable) {
      fireTile.hidden = !fireAvailable;
      railChanged = true;
    }
    const aqAvailable = aqLoaded !== null && !hidden;
    const alertsAvailable = alertsLoaded !== null && !hidden;
    if (aqTile.hidden === aqAvailable) {
      aqTile.hidden = !aqAvailable;
      railChanged = true;
    }
    if (alertsTile.hidden === alertsAvailable) {
      alertsTile.hidden = !alertsAvailable;
      railChanged = true;
    }
    if (railChanged) syncRailDensity();
    const drawSoundings = soundingAvailable && view.marks.stations.soundings;
    const drawAirports = airportAvailable && view.marks.stations.airports;
    const drawSynop = synopAvailable && view.marks.stations.synop;
    const drawFire = fireAvailable && view.marks.stations.fire;
    const drawAq = aqAvailable && view.marks.stations.aq;
    syncRail();
    applyFireView(drawFire);
    applyAlertsView(alertsAvailable && view.marks.stations.alerts);
    // Nothing on screen and nothing on the map: the layers are never added,
    // so a viewer who asks for no station pays nothing for the products
    // existing.
    if (!drawSoundings && !drawAirports && !drawSynop && !drawAq && stationLayers === null) return;
    const layers = ensureStationLayers();
    if (!layers) return;
    layers.setSoundings(drawSoundings ? soundingLoaded!.index : null);
    layers.setAirports(drawAirports ? airportLoaded!.index : null);
    layers.setSynop(drawSynop ? synopLoaded!.index : null);
    layers.setAq(drawAq ? aqLoaded!.index : null);
    syncStationTime();
    closeStationCard();
  }

  /** The fire layer is added the first time fires are asked for and stays,
   * empty, when they are switched off, like the station layers. */
  function applyFireView(draw: boolean): void {
    if (!draw && fireLayers === null) return;
    if (!deps.mapStyleReady) return;
    if (!fireLayers) {
      fireLayers = new FireLayers(map, (error) => console.warn("fire: detections not read:", error));
    }
    const before = firstSymbolLayerId(map);
    fireLayers.ensure(before);
    fireLayers.setInk(document.body.dataset.ground === "dark");
    // A case's round tells its story: the whole window up to the playhead.
    fireLayers.setHistory(deps.activeCase !== null);
    fireLayers.onPassChange = syncFireWindArrow;
    fireLayers.setTime(fireWind.time ?? stationTime());
    fireLayers.setIndex(draw ? (deps.activeCase !== null ? deps.caseFireLoaded : fireLoaded) : null);
    fireWind.arrow ??= new FireWindArrow(map);
    // On top of the map, so its label is placed before the places' and the
    // marks' and keeps its room.
    fireWind.arrow.ensure();
    syncFireWindArrow();
  }

  /** The warnings in force, drawn while their tile is pressed: the round's
   * network files are read once per round (one GET each), and the layers
   * rebuilt only when the round or its lines change. */
  function applyAlertsView(draw: boolean): void {
    if (!draw && alertLayers === null) return;
    if (!deps.mapStyleReady) return;
    alertLayers ??= new AlertLayers(map);
    alertLayers.setBefore(firstSymbolLayerId(map));
    alertLayers.setInk(document.body.dataset.ground === "dark");
    const loaded = draw ? alertsLoaded : null;
    if (loaded && alertLines?.indexUrl !== loaded.indexUrl) {
      const lines: AlertLine[][] = loaded.index.networks.map(() => []);
      alertLines = { indexUrl: loaded.indexUrl, lines };
      const pending = loaded.index.networks.map((network, position) =>
        fetchAlertLines(loaded, network)
          .then((read) => {
            lines[position] = read;
          })
          .catch((error: unknown) => {
            console.warn(`alerts: ${network.id} not read:`, error instanceof Error ? error.message : error);
          }),
      );
      void Promise.all(pending).then(() => {
        if (alertLines?.lines !== lines) return;
        alertLines = { indexUrl: loaded.indexUrl, lines: [...lines] };
        applyAlertsView(alertsLoaded !== null && deps.activeCase === null && view.marks.stations.alerts);
      });
    }
    const next = loaded && alertLines?.indexUrl === loaded.indexUrl ? alertLines : null;
    if (next === alertsDrawn) return;
    alertsDrawn = next;
    if (next && loaded) alertLayers.setData(loaded, next.lines);
    else alertLayers.setData(null, []);
    // The pin's chip reads the rendered areas, which are there once drawn.
    if (probePanelControl.probe) {
      resolveProbeAlerts();
      map.once("idle", resolveProbeAlerts);
    }
  }

  /** Ask for the wind the case's fire marks are read against, once per
   * case: its own `wind10m`, else the bundle of the case its `wind` block
   * names, opened beside the case's run like a mosaic member is. */
  function applyFireWind(): void {
    const showcaseCase = deps.activeCase;
    const run = deps.manifest;
    if (!showcaseCase || !run || fireWind.forCase === showcaseCase.id) return;
    fireWind.forCase = showcaseCase.id;
    const own = hasBundle(run, "wind10m");
    const lender = own ? undefined : showcaseCase.wind?.case;
    if (!own && lender === undefined) return;
    const sequence = deps.initializeSequence;
    void (async () => {
      let member: BorrowedRun | null = null;
      if (lender !== undefined) {
        const catalog = await fetchCatalog(dataBaseUrl());
        const other = catalog.cases.find((item) => item.id === lender);
        if (!other) throw new Error(`case ${lender} is not in the catalog`);
        const loaded = await fetchCaseManifest(dataBaseUrl(), other);
        if (sequence !== deps.initializeSequence) return;
        member = {
          id: other.modelId,
          manifest: loaded.manifest,
          manifestUrl: loaded.manifestUrl,
          run: other.run,
          manifestCrc: other.manifestCrc32,
          band: { start: 0, width: 360 },
          slot: null,
          wanted: null,
        };
      }
      const session = await loadVariable("wind10m", sequence, "overlay", member);
      if (sequence !== deps.initializeSequence) return;
      fireWind.session = session;
      fireWind.borrowed = member !== null;
      if (member) {
        // The lender's notice joins the credit line (`data-wind-credit`).
        document.body.dataset.windCredit = member.id;
        if (deps.activeSession && !deps.activeSession.vector) {
          particlesToggle.hidden = false;
          syncRailDensity();
        }
        if (deps.activeFrameIndex !== null) trySelectFireWind(deps.activeFrameIndex);
      }
      syncFireWindArrow();
    })().catch((error: unknown) => {
      if (sequence !== deps.initializeSequence) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      console.warn("fire: case wind unavailable:", error instanceof Error ? error.message : error);
    });
  }

  /** Forget the case's wind: a new dataset. Its session ends with the run's
   * (or the mosaic members', for a borrowed one). */
  function resetFireWind(): void {
    fireWind.session = null;
    fireWind.borrowed = false;
    fireWind.forCase = null;
    fireWind.series.clear();
    fireWind.requests.clear();
    fireWind.wantedKeys.clear();
    fireWind.shownKeys = [];
    fireWind.time = null;
    fireWind.arrow?.show(null);
    delete document.body.dataset.windCredit;
  }

  /** Run the particles off a borrowed wind for the primary frame at `index`:
   * the lender's latest frame not later than it, held while its planes
   * decode. A vector primary runs them off its own planes instead. */
  function trySelectFireWind(index: number): void {
    const session = fireWind.session;
    if (!fireWind.borrowed || !session || !deps.layersAdded) return;
    if (!deps.activeSession || deps.activeSession.vector || !view.particles) {
      fireWind.shownKeys = [];
      return;
    }
    const offset = memberOffsetForFrame(session, index);
    if (offset === null) {
      deps.windLayer?.setVisible(false);
      fireWind.shownKeys = [];
      return;
    }
    const variables = session.variables.slice(0, 2);
    if (variables.length < 2) return;
    const keys = variables.map((variable) => cacheKey(session, variable, offset));
    const frames = keys.map((key) => planeCache.get(key));
    let complete = true;
    for (const [position, frame] of frames.entries()) {
      if (frame && frame.tiles === null) continue;
      complete = false;
      fireWind.wantedKeys.add(keys[position]!);
      requestDecode(session, variables[position]!, offset);
    }
    if (!complete) return;
    for (const key of keys) fireWind.wantedKeys.delete(key);
    fireWind.shownKeys = keys;
    ensureWindLayer();
    ensureWindGrid(session);
    refreshParticleInk();
    deps.windLayer!.setWindPlanes(frames[0]!.plane, frames[1]!.plane);
    deps.windLayer!.setVisible(true);
  }

  /** The case wind's valid times, epoch ms, in the order its series come. */
  function fireWindTimes(session: VariableSession): number[] {
    const time = session.metadata.time;
    const unit = axisUnitSeconds(time) * 1000;
    const base = Date.parse(session.metadata.runTime);
    return frameOffsets(time).map((offset) => base + offset * unit);
  }

  /** The case wind's u, v series at a cell, asked for the first time it is
   * wanted; null until both components have landed. */
  function fireWindSeries(session: VariableSession, cell: number): { u: Float32Array; v: Float32Array } | null {
    const entry = fireWind.series.get(cell);
    if (entry?.u && entry.v) return { u: entry.u, v: entry.v };
    if (entry || !session.tiles || !deps.ready) return null;
    fireWind.series.set(cell, { u: null, v: null });
    const width = session.metadata.grid.width;
    for (const [component, variable] of session.variables.slice(0, 2).entries()) {
      const requestId = deps.nextRequestId++;
      fireWind.requests.set(requestId, { cell, component: component as 0 | 1 });
      session.worker.postMessage({
        type: "series",
        requestId,
        generation: deps.generation,
        variableId: variable.numericId,
        column: cell % width,
        row: Math.floor(cell / width),
      });
    }
    return null;
  }

  /** A series the case wind asked for, decoded and filed; false for one it
   * did not ask for (the probe's). */
  function acceptFireWindSeries(
    session: VariableSession,
    message: { requestId?: number; generation: number; variableId: number; buffer: ArrayBuffer },
  ): boolean {
    const request = typeof message.requestId === "number" ? fireWind.requests.get(message.requestId) : undefined;
    if (!request) return false;
    fireWind.requests.delete(message.requestId!);
    const variable = session.variables.find((item) => item.numericId === message.variableId);
    const entry = fireWind.series.get(request.cell);
    if (session !== fireWind.session || message.generation !== deps.generation || !variable || !entry) return true;
    const table = decodeTable(variable);
    const codes = new Uint8Array(message.buffer);
    const values = new Float32Array(codes.length);
    for (let i = 0; i < codes.length; i++) values[i] = table[codes[i]!]!;
    if (request.component === 0) entry.u = values;
    else entry.v = values;
    syncFireWindArrow();
    return true;
  }

  /** Draw the mean wind at the newest pass, from its start to the marks'
   * time, or nothing without a pass, a wind or the fire marks. */
  function syncFireWindArrow(): void {
    const arrow = fireWind.arrow;
    if (!arrow) return;
    const pass = fireLayers?.latestPass() ?? null;
    const session = fireWind.session;
    if (!pass || !session) {
      arrow.show(null);
      return;
    }
    const cell = nearestCell(geoGrid(session.metadata), pass.center[0], pass.center[1]);
    if (cell < 0) {
      arrow.show(null);
      return;
    }
    const series = fireWindSeries(session, cell);
    // Whatever is drawn stays until the series lands.
    if (!series) return;
    const mean = meanWind(fireWindTimes(session), series.u, series.v, pass.start, fireWind.time ?? stationTime());
    arrow.show(mean ? { center: pass.center, u: mean.u, v: mean.v, from: pass.start } : null);
  }

  /** The valid time the marks are dimmed against: the playhead's, or now
   * before a run has loaded. */
  function stationTime(): number {
    const index = deps.activeFrameIndex ?? deps.requestedFrameIndex;
    return deps.metadata && index !== null ? frameValidTime(index) : Date.now();
  }

  function syncStationTime(): void {
    syncRadarTime();
    fireWind.time = stationTime();
    fireLayers?.setTime(fireWind.time);
    syncFireWindArrow();
    deps.caseMarkers?.setTime(stationTime());
    syncCaseCaption();
    if (!stationLayers) return;
    stationLayers.setTime(stationTime());
  }

  function firePointAt(point: { x: number; y: number }): FireDetection | { cluster: number; lngLat: [number, number] } | null {
    if (!fireLayers) return null;
    const layers = FIRE_CLICKABLE_LAYERS.filter((id) => map.getLayer(id) !== undefined);
    if (!layers.length) return null;
    const box: [[number, number], [number, number]] = [
      [point.x - 5, point.y - 5],
      [point.x + 5, point.y + 5],
    ];
    for (const feature of map.queryRenderedFeatures(box, { layers })) {
      const clusterId = feature.properties?.cluster_id;
      if (typeof clusterId === "number" && feature.geometry.type === "Point")
        return { cluster: clusterId, lngLat: feature.geometry.coordinates as [number, number] };
      const detection = fireLayers.detectionOf(feature);
      if (detection) return detection;
    }
    return null;
  }

  /** A cluster opens by zooming to where it splits; a detection opens its
   * card in the station card's place. */
  function openFire(hit: FireDetection | { cluster: number; lngLat: [number, number] }): void {
    closeStationCard();
    if ("cluster" in hit) {
      void fireLayers
        ?.clusterZoom(hit.cluster)
        .then((zoom) => map.easeTo({ center: hit.lngLat, zoom: Math.max(zoom, map.getZoom() + 1) }))
        .catch(() => map.easeTo({ center: hit.lngLat, zoom: map.getZoom() + 2 }));
      return;
    }
    stationCard = cardPopup({ className: "station-popup", maxWidth: "280px", offset: 8 })
      .setLngLat([hit.lon, hit.lat])
      .setDOMContent(buildFireCard(hit, { formatTime: (time) => formatDate(time) }))
      .addTo(map);
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
    let content: HTMLElement;
    if (data.kind === "aq") {
      const loaded = aqLoaded;
      if (!loaded) return;
      const card = buildAqCard(data.station, data.network, loaded.index, options);
      content = card.root;
      // The window adds the day's range and the station's own spelling of
      // its name: one range request, made when the card opens.
      const station = loaded.index.stations.find((entry) => entry.id === data.station.id);
      if (station) {
        void fetchAqStation(loaded, station)
          .then((series) => card.setSeries(series))
          .catch((error: unknown) => {
            console.warn(`aq: ${station.id} window not read:`, error instanceof Error ? error.message : error);
          });
      }
    } else {
      content =
        data.kind === "sounding"
          ? buildSoundingCard(data.station, options)
          : data.kind === "synop"
            ? buildSynopCard(data.station, data.network, options)
            : buildAirportCard(data.station, options);
    }
    stationCard = cardPopup({ className: "station-popup", maxWidth: "280px", offset: 10 })
      .setLngLat(lngLat)
      .setDOMContent(content)
      .addTo(map);
  }

  /** The warnings card at a clicked point: every warning drawn there. */
  function showAlertsCard(hits: AlertHit[], lngLat: [number, number]): void {
    closeStationCard();
    if (!alertsLoaded) return;
    stationCard = cardPopup({ className: "station-popup", maxWidth: "300px", offset: 6 })
      .setLngLat(lngLat)
      .setDOMContent(
        buildAlertsCard(hits, {
          issued: alertsLoaded.index.issued,
          formatTime: (time) => formatDate(time),
          darkGround: document.body.dataset.ground === "dark",
        }),
      )
      .addTo(map);
  }

  function closeStationCard(): void {
    stationCard?.remove();
    stationCard = null;
  }

  /** Hold a card opened elsewhere (a case marker's) in the station card's
   * place, so the next mark or storm card closes it. */
  function adoptStationCard(card: Popup): void {
    stationCard = card;
  }

  /** Each tile is its own switch: pressing it draws that product, pressing
   * it again takes it off, and the address bar carries what is on. */
  function toggleStationProduct(product: keyof StationsUrlState): void {
    view.marks.stations = { ...view.marks.stations, [product]: !view.marks.stations[product] };
    syncUrl();
    applyStationView();
  }

  return {
    fireWind,
    syncFireWindArrow,
    syncTcTime,
    syncStationTime,
    trySelectFireWind,
    renderTcLevelGroup,
    applyTcView,
    applyStationView,
    stationTime,
    closeStationCard,
    closeTcCard,
    adoptStationCard,
    acceptFireWindSeries,
    toggleStationProduct,
    applyFireWind,
    resetFireWind,
    renderTcSheet,
    tcPointAt,
    showTcCard,
    stationPointAt,
    showStationCard,
    firePointAt,
    openFire,
    showAlertsCard,
    loadTc,
    loadStations,
    get tcLayers() {
      return tcLayers;
    },
    get stationLayers() {
      return stationLayers;
    },
    get fireLayers() {
      return fireLayers;
    },
    get alertLayers() {
      return alertLayers;
    },
    get tcLoaded() {
      return tcLoaded;
    },
    get soundingLoaded() {
      return soundingLoaded;
    },
    get airportLoaded() {
      return airportLoaded;
    },
    get synopLoaded() {
      return synopLoaded;
    },
    get aqLoaded() {
      return aqLoaded;
    },
  };
}
