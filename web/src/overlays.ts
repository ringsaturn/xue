// The overlays that follow the playhead beside the field: the single-site
// radar (radar/) and a case's fire, river and context layers (fire/,
// river/, casecontext.ts). Those hold the layers and the reads; this is
// the controller.
import type { Map as MaplibreMap } from "maplibre-gl";
import { type BasemapStyle, FORECAST_ANCHOR_LAYER } from "./basemap";
import { CaseContextLayers, type ContextCollection, fetchCaseContext } from "./casecontext";
import { fetchImmutable } from "./fetchimmutable";
import { fetchCaseFireIndex, type LoadedFireIndex } from "./fire/fetch";
import { htmlLang, t } from "./i18n";
import type { ForecastLayer } from "./layer";
import type { BundleMetadata, ForecastBundleId } from "./manifest";
import { firstSymbolLayerId } from "./marks";
import { applyPageMeta } from "./pagemeta";
import { buildPalette } from "./palettes";
import { type RadarFeed, windowFeed } from "./radar/feed";
import { liveFeed } from "./radar/live";
import { RADAR_SITE_LAYER, RadarOverlay, type RadarReadout } from "./radar/overlay";
import { GATE_KM, GATES, parseRadarWindow } from "./radar/schema";
import { RiverCase } from "./river/case";
import { fetchRiverIndex } from "./river/index";
import type { VariableSession } from "./session";
import type { ShowcaseCase } from "./showcase-catalog";
import { dataBaseUrl } from "./site";
import { convertLegendLabel, displayUnit } from "./units";
import { displayZone, formatStamp } from "./timezone";
import type { DerivedFireId } from "./urlstate";
import type { LegendSwatch } from "./variables";
import type { ViewState } from "./viewstate";

/** What the overlays read off the page: the title, legend and rail
 * elements the radar writes into, the case and the playhead (live,
 * through getters), and the page functions a radar switch reaches back
 * into. */
export interface OverlaysDeps {
  readonly appliedBasemapStyle: BasemapStyle;
  readonly map: MaplibreMap;
  readonly runTime: HTMLElement;
  readonly runTimeLabel: HTMLElement;
  readonly variableCode: HTMLElement;
  readonly variableTitle: HTMLElement;
  readonly legend: HTMLElement;
  readonly legendBar: HTMLElement;
  readonly legendUnit: HTMLElement;
  readonly legendLabels: HTMLElement;
  readonly levelRow: HTMLElement;
  readonly radarTile: HTMLButtonElement;
  readonly legendFolded: HTMLElement;
  readonly modelEyebrow: HTMLElement;
  readonly caseRiverPanel: HTMLElement;
  readonly riverCredit: HTMLElement;
  readonly contextCredit: HTMLElement;
  readonly metadata: BundleMetadata | null;
  readonly slots: { readonly fill: { readonly layer: Pick<ForecastLayer, "setSuppressed"> } };
  readonly activeCase: ShowcaseCase | null;
  readonly activeSession: VariableSession | null;
  readonly activeFrameIndex: number | null;
  readonly requestedFrameIndex: number | null;
  readonly sessions: Map<string, VariableSession>;
  readonly view: ViewState;
  readonly selectedVariableId: string;
  readonly sayText: (element: HTMLElement, text: string) => void;
  readonly applyDatasetWording: () => void;
  readonly frameValidTime: (index: number) => number;
  readonly scheduleProbeRender: () => void;
  readonly caseImageryOn: () => boolean;
  readonly syncRailDensity: () => void;
  readonly syncRail: () => void;
  readonly renderLegendKey: (key: readonly LegendSwatch[] | null) => void;
  readonly updateVariablePresentation: (session: VariableSession) => void;
  readonly applyCaseCamera: (showcaseCase: ShowcaseCase, recenter: boolean) => void;
  readonly syncUrl: () => void;
  readonly mapStyleReady: boolean;
  readonly applyStationView: () => void;
  readonly stationTime: () => number;
  readonly fireField: () => DerivedFireId | null;
}

/** The overlays controller: the single-site radar (live or a case's
 * window) and a case's fire detections, river and context lines. */
export function createOverlays(deps: OverlaysDeps) {
  const {
    map,
    runTime,
    runTimeLabel,
    variableCode,
    variableTitle,
    legend,
    legendBar,
    legendUnit,
    legendLabels,
    levelRow,
    radarTile,
    legendFolded,
    modelEyebrow,
    caseRiverPanel,
    riverCredit,
    contextCredit,
    slots,
    sessions,
    view,
    sayText,
    applyDatasetWording,
    frameValidTime,
    scheduleProbeRender,
    caseImageryOn,
    syncRailDensity,
    syncRail,
    renderLegendKey,
    updateVariablePresentation,
    applyCaseCamera,
    syncUrl,
    applyStationView,
    stationTime,
  } = deps;
  // ---------------------------------------------------------------------------
  // Single-site radar (docs/nexrad.md): an overlay that follows the playhead,
  // like the station marks, drawn from the live source bucket's Level 3 sweeps
  // or a showcase case's window of polar stores. One site at a time:
  // the rail tile turns it on, a click on a site's mark picks the site, and
  // the chip under the title switches reflectivity and velocity. The link
  // carries `?radar=<site>` and `?radarproduct=n0g`.

  /** Whether the link named a radar state: a case's default then does not
   * override it. */
  const requestedRadarFromUrl = new URLSearchParams(window.location.search).has("radar");
  const requestedStationsFromUrl = new URLSearchParams(window.location.search).has("stations");

  /** Whether one radar site is drawn alone (`applyRadarView`): with a fire
   * field, one of the two reasons the fill is held off screen. */
  let radarAlone = false;
  /** Where the overlay reads its sites and sweeps: the live source, or the
   * case's window. */
  let radarFeed: RadarFeed | null = null;
  /** How far a site's longest-reaching product sees, km: N0B's 460. */
  const RADAR_REACH_KM = Math.max(...Object.values(GATES)) * GATE_KM;
  let radarOverlay: RadarOverlay | null = null;
  /** The case's window, read once per case. */
  let radarCaseLoadedFor: string | null = null;

  /** The live feed costs nothing until a site is opened: its sites are a
   * static table, and only an open site lists and reads the bucket. */
  function loadLiveRadar(): void {
    if (radarFeed?.live) return;
    radarFeed = liveFeed();
    applyRadarView();
  }

  /** The case's own fire round, read once per case. */
  let caseFireLoaded: LoadedFireIndex | null = null;
  let fireCaseLoadedFor: string | null = null;

  async function loadCaseFire(showcaseCase: ShowcaseCase): Promise<void> {
    const fire = showcaseCase.fire;
    if (!fire || fireCaseLoadedFor === showcaseCase.id) return;
    fireCaseLoadedFor = showcaseCase.id;
    try {
      caseFireLoaded = await fetchCaseFireIndex(dataBaseUrl(), fire.indexPath, fire.crc32, fire.byteLength);
      // A case's detections are its story: on unless the link says otherwise.
      if (!requestedStationsFromUrl) view.marks.stations = { ...view.marks.stations, fire: true };
    } catch (error) {
      console.warn(`fire: case ${showcaseCase.id} round not read:`, error instanceof Error ? error.message : error);
      caseFireLoaded = null;
    }
    applyStationView();
  }

  /** The case's river data (`docs/river.md`), read once per case: the
   * reaches, gauges and extents on the map, the block in the case panel,
   * the credits on the credit line. An index this shell cannot read is a
   * warning, and the case plays its field alone. */
  let riverCase: RiverCase | null = null;
  let riverCaseLoadedFor: string | null = null;

  async function loadCaseRiver(showcaseCase: ShowcaseCase): Promise<void> {
    const river = showcaseCase.river;
    if (!river || riverCaseLoadedFor === showcaseCase.id || !deps.mapStyleReady) return;
    riverCaseLoadedFor = showcaseCase.id;
    const loaded = await fetchRiverIndex(dataBaseUrl(), river);
    if (!loaded || deps.activeCase?.id !== showcaseCase.id) return;
    riverCase = new RiverCase({
      map,
      loaded,
      bbox: showcaseCase.bbox,
      byRamp: river.color !== undefined,
      onChange: () => {
        syncRiverCredit();
        scheduleProbeRender();
      },
    });
    try {
      riverCase.start();
    } catch (error) {
      console.warn("river: layers not added:", error instanceof Error ? error.message : error);
      riverCase.dispose();
      riverCase = null;
      return;
    }
    riverCase.setInk(document.body.dataset.ground === "dark");
    riverCase.setTime(stationTime());
    riverCase.renderPanel(caseRiverPanel);
    syncRiverCredit();
    syncRiverRamp();
  }

  /** Hand the reaches the palette of the bundle the row's `river.color`
   * names, once a session has opened it: the field's own colours, the
   * page's declared palette included. Until then a reach coloured that way
   * is not drawn. */
  function syncRiverRamp(): void {
    const color = deps.activeCase?.river?.color;
    if (!riverCase || color === undefined) return;
    const session = sessions.get(color as ForecastBundleId);
    if (!session) return;
    riverCase.setRamp({ palette: buildPalette(session.variable, session.identity), quantization: session.variable.quantization });
  }

  /** The credit line's river notice: the index's credits and each extent
   * on screen, while the case's river data is up. */
  function syncRiverCredit(): void {
    if (!riverCase) {
      delete document.body.dataset.river;
      riverCredit.textContent = "";
      return;
    }
    document.body.dataset.river = "";
    riverCredit.textContent = `${riverCase.credits().join(" · ")} ·`;
  }

  /** The case's context lines (`casecontext.ts`), read once per case. A case
   * opened in place of another takes the last one's lines and credit off
   * first; a file this shell cannot read is a warning, and the case plays
   * without them. */
  let caseContext: CaseContextLayers | null = null;
  let contextCaseLoadedFor: string | null = null;
  let contextCreditText: string | null = null;

  async function loadCaseContext(showcaseCase: ShowcaseCase): Promise<void> {
    if (contextCaseLoadedFor === showcaseCase.id || !deps.mapStyleReady) return;
    contextCaseLoadedFor = showcaseCase.id;
    caseContext?.remove();
    contextCreditText = null;
    syncContextCredit();
    const context = showcaseCase.context;
    if (!context) return;
    let features: ContextCollection;
    try {
      features = await fetchCaseContext(dataBaseUrl(), context);
    } catch (error) {
      console.warn(`context: case ${showcaseCase.id} lines not read:`, error instanceof Error ? error.message : error);
      return;
    }
    // Another case opened while this one's file was in flight.
    if (deps.activeCase?.id !== showcaseCase.id || contextCaseLoadedFor !== showcaseCase.id) return;
    caseContext ??= new CaseContextLayers(map, {
      anchorLayer: FORECAST_ANCHOR_LAYER,
      isBasemapLayer: (id) => deps.appliedBasemapStyle.layers.some((layer) => layer.id === id),
    });
    caseContext.setInk(document.body.dataset.ground === "dark");
    try {
      caseContext.show(features);
    } catch (error) {
      console.warn("context: layers not added:", error instanceof Error ? error.message : error);
      caseContext.remove();
      return;
    }
    contextCreditText = context.credit;
    syncContextCredit();
  }

  /** The credit line's notice for the case's context lines, while they are
   * on the map. */
  function syncContextCredit(): void {
    if (contextCreditText === null) {
      delete document.body.dataset.context;
      contextCredit.textContent = "";
      return;
    }
    document.body.dataset.context = "";
    contextCredit.textContent = `${contextCreditText};`;
  }

  async function loadCaseRadar(showcaseCase: ShowcaseCase): Promise<void> {
    const radar = showcaseCase.radar;
    if (!radar || radarCaseLoadedFor === showcaseCase.id) return;
    radarCaseLoadedFor = showcaseCase.id;
    const url = new URL(radar.windowPath, new URL(dataBaseUrl(), document.baseURI));
    url.searchParams.set("v", radar.crc32);
    try {
      const response = await fetchImmutable(url);
      if (!response.ok) throw new Error(`radar window request failed: ${response.status}`);
      radarFeed = windowFeed(parseRadarWindow(await response.json()), url.href);
      // A case opens with its radar on, on the site and product it names,
      // unless the link says otherwise.
      if (!requestedRadarFromUrl) {
        view.marks.radar = { on: true, site: radar.defaultSite, product: radar.defaultProduct };
      }
    } catch (error) {
      console.warn(`radar: case ${showcaseCase.id} window not read:`, error instanceof Error ? error.message : error);
      radarFeed = null;
    }
    if (deps.activeCase?.id === showcaseCase.id) applyCaseCamera(showcaseCase, false);
    applyRadarView();
  }

  function ensureRadarOverlay(): RadarOverlay | null {
    if (!deps.mapStyleReady) return null;
    if (!radarOverlay) radarOverlay = new RadarOverlay(map, renderRadarChip);
    const before = firstSymbolLayerId(map);
    radarOverlay.ensure(before);
    const style = getComputedStyle(document.body);
    radarOverlay.setInk(
      style.getPropertyValue("--text").trim() || "#111111",
      style.getPropertyValue("--bg").trim() || "#ffffff",
    );
    return radarOverlay;
  }

  function applyRadarView(): void {
    // Live, the sites belong to the reflectivity alone: over a temperature or
    // a wind a radome mark reads as data the field does not have. A case's
    // radar is part of its story and stays on whatever field is up.
    const available =
      radarFeed !== null &&
      (deps.activeCase === null
        ? radarFeed.live && deps.selectedVariableId === "cref"
        : deps.activeCase.radar !== undefined && !radarFeed.live);
    if (radarTile.hidden === available) {
      radarTile.hidden = !available;
      syncRailDensity();
    }
    syncRail();
    const state = view.marks.radar;
    if (!available || !state.on) {
      radarOverlay?.remove();
      radarOverlay = null;
      renderRadarChip(null);
      setRadarAlone(false);
      return;
    }
    const overlay = ensureRadarOverlay();
    if (!overlay) return;
    overlay.setFeed(radarFeed);
    // A site the feed does not have is no site: the marks show, the mosaic
    // stays.
    const site = state.site && radarFeed!.sites.some((entry) => entry.id === state.site) ? state.site : null;
    if (site !== state.site) view.marks.radar = { ...state, site };
    overlay.setProduct(view.marks.radar.product);
    overlay.setSite(site);
    // One radar, alone: a chosen site's sweep is read against the basemap,
    // not blended over a mosaic of every other radar.
    setRadarAlone(site !== null);
    syncRadarTime();
  }

  function setRadarAlone(alone: boolean): void {
    radarAlone = alone;
    document.body.classList.toggle("radar-alone", alone);
    syncFillSuppressed();
  }

  /** Hold the fill off screen while something stands in for it: one radar
   * site drawn alone, a fire field drawn on the fill's session, or a case's
   * daily picture. */
  function syncFillSuppressed(): void {
    const fire = deps.fireField();
    slots.fill.layer.setSuppressed(radarAlone || fire !== null || caseImageryOn());
    document.body.classList.toggle("fire-field", fire !== null);
  }

  function syncRadarTime(): void {
    if (!radarOverlay) return;
    const index = deps.activeFrameIndex ?? deps.requestedFrameIndex;
    radarOverlay.setTime(deps.metadata && index !== null ? frameValidTime(index) : null);
  }

  /** The scale of the sweep on screen, written into the field legend while a
   * site is drawn alone: the reflectivity ramp the shader draws from 5 dBZ,
   * or velocity ±30 m/s from green (toward the radar) through pale to red
   * (away), with the range-folded tint under it. Top first, as the legend's
   * ticks read. */
  const RADAR_SCALES = {
    n0b: {
      unit: "dBZ",
      labels: ["65", "45", "25", "5"],
      gradient: "linear-gradient(to bottom, rgb(204 0 204), rgb(230 0 0), rgb(255 140 0), rgb(255 230 0), rgb(0 140 0), rgb(0 217 0), rgb(0 153 242), rgb(99 235 235))",
    },
    n0g: {
      unit: "m/s",
      labels: ["+30", "0", "−30"],
      gradient: "linear-gradient(to bottom, rgb(158 0 0), rgb(224 189 189) 50%, rgb(184 217 184) 50%, rgb(0 115 0))",
    },
  } as const;

  /** The radar's group on the level row: the sweep on screen as its caption,
   * then the site as a pressed chip whose ✕ is the way back to the mosaic.
   * Built once per site and updated in place, since the caption follows
   * every frame. The products are on the rail. */
  let radarLevelGroup: { root: HTMLElement; caption: HTMLElement; site: HTMLButtonElement } | null = null;

  /** REF and VEL on the rail, under the radar tile: shown while a site is. */
  const radarProductTiles = [...document.querySelectorAll<HTMLButtonElement>("[data-radar-product]")];
  for (const tile of radarProductTiles) {
    const product = tile.dataset.radarProduct as "n0b" | "n0g";
    tile.addEventListener("click", () => {
      if (view.marks.radar.product !== product) setRadarProduct(product);
    });
  }

  function syncRadarProductTiles(product: "n0b" | "n0g" | null): void {
    let changed = false;
    for (const tile of radarProductTiles) {
      if (tile.hidden !== (product === null)) {
        tile.hidden = product === null;
        changed = true;
      }
      tile.setAttribute("aria-pressed", String(tile.dataset.radarProduct === product));
    }
    if (changed) syncRailDensity();
  }

  function ensureRadarLevelGroup(): NonNullable<typeof radarLevelGroup> {
    if (radarLevelGroup && radarLevelGroup.root.isConnected) return radarLevelGroup;
    const root = document.createElement("div");
    root.className = "level-group";
    root.dataset.slot = "radar";
    const caption = document.createElement("span");
    caption.className = "level-caption";
    const site = document.createElement("button");
    site.type = "button";
    site.dataset.level = "";
    site.dataset.slot = "radar";
    site.className = "is-removable";
    site.setAttribute("aria-pressed", "true");
    site.addEventListener("click", () => closeRadarSite());
    root.append(caption, site);
    levelRow.append(root);
    radarLevelGroup = { root, caption, site };
    return radarLevelGroup;
  }

  /** The readout last drawn, so a field change under an open site can put
   * the radar's title back over the field's. */
  let lastRadarReadout: RadarReadout | null = null;
  /** The field's own stamp, kept while a site's sweep time stands in it. */
  let fieldStampText = "";

  function stampField(text: string): void {
    fieldStampText = text;
    if (!legend.dataset.radar) sayText(runTime, text);
  }

  const RADAR_TITLES = { n0b: "Base Reflectivity", n0g: "Base Velocity" } as const;
  const RADAR_CODES = { n0b: "N0B", n0g: "N0G" } as const;

  /** The headline names what is on screen: with a site open, its product and
   * the radar, in the instrument English every headline is in; the page's
   * own title, which is localized, follows on a live view. */
  function applyRadarTitle(readout: RadarReadout): void {
    variableTitle.textContent = RADAR_TITLES[readout.product];
    variableCode.textContent = `NEXRAD / ${readout.site.icao} ${RADAR_CODES[readout.product]}`;
    modelEyebrow.textContent = "NOAA / NWS NEXRAD";
    runTimeLabel.textContent = t("latestObservation");
    if (readout.sweepTime !== null) sayText(runTime, formatStamp(readout.sweepTime, "UTC"));
    if (!deps.activeCase) {
      applyPageMeta({
        path: "/",
        title: t("pageTitleLiveObservation", {
          variable: t(readout.product === "n0b" ? "radarReflectivity" : "radarVelocity"),
          model: `NEXRAD ${readout.site.icao}`,
          hours: String(Math.round(radarWindowHours())),
        }),
        description: t("metaDescription"),
      });
    }
  }

  function radarWindowHours(): number {
    return radarFeed ? radarFeed.windowSeconds / 3600 : 3;
  }

  function renderRadarChip(readout: RadarReadout | null): void {
    lastRadarReadout = readout;
    if (!readout) {
      radarLevelGroup?.root.remove();
      radarLevelGroup = null;
      levelRow.hidden = levelRow.childElementCount === 0;
      syncRadarProductTiles(null);
      restoreFieldLegend();
      return;
    }
    syncRadarProductTiles(readout.product);
    const group = ensureRadarLevelGroup();
    levelRow.hidden = false;
    // The sweep's clock time alone: the capsule beside it already carries the
    // date, and a phone's row has room for little more than the products.
    const time =
      readout.sweepTime === null
        ? "--"
        : new Intl.DateTimeFormat(htmlLang, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: displayZone }).format(
            readout.sweepTime,
          );
    // How far the sweep trails the playhead: a radar scans every few minutes
    // and the playhead moves in the mosaic's steps, so the two clocks differ
    // by design. Past the window the sweep is the site's newest, however old.
    const minutes = readout.age === null || readout.stale ? null : Math.round(readout.age / 60_000);
    const age = minutes === null ? "" : minutes === 0 ? " ±0m" : ` −${minutes}m`;
    // The age goes first on a phone: the time and the site chip are what the
    // row has room for.
    const when = document.createElement("span");
    when.textContent = `${readout.stale ? `${t("radarLatest")} ` : ""}${time}${readout.loading ? " …" : ""}`;
    const trailing = document.createElement("span");
    trailing.className = "level-caption-age";
    trailing.textContent = readout.loading ? "" : age;
    group.caption.replaceChildren(when, trailing);
    group.site.textContent = readout.site.icao;
    group.site.title = t("radarCloseAria");
    group.site.setAttribute("aria-label", `${readout.site.icao}: ${t("radarCloseAria")}`);
    renderRadarLegend(readout.product);
    applyRadarTitle(readout);
  }

  /** The field legend, showing the radar's scale while a site is alone. */
  function renderRadarLegend(product: "n0b" | "n0g", force = false): void {
    const scale = RADAR_SCALES[product];
    if (legend.dataset.radar === product && !force) return;
    legend.dataset.radar = product;
    renderLegendKey(null);
    legendUnit.textContent = displayUnit(scale.unit);
    legendLabels.replaceChildren(...scale.labels.map((label) => {
      const span = document.createElement("span");
      span.textContent = convertLegendLabel(scale.unit, label);
      return span;
    }));
    legendBar.style.background = scale.gradient;
    legendFolded.hidden = false;
  }

  /** Give the legend back to the field on screen. */
  function restoreFieldLegend(): void {
    if (legend.dataset.radar === undefined) return;
    delete legend.dataset.radar;
    legendFolded.hidden = true;
    if (fieldStampText) sayText(runTime, fieldStampText);
    applyDatasetWording();
    // The field's own headline, legend and page title.
    if (deps.activeSession) updateVariablePresentation(deps.activeSession);
  }

  /** Back to the field: the site closes, the sites stay marked. */
  function closeRadarSite(): void {
    view.marks.radar = { ...view.marks.radar, site: null };
    syncUrl();
    applyRadarView();
  }

  function radarSiteAt(point: { x: number; y: number }): string | null {
    if (!radarOverlay || !map.getLayer(RADAR_SITE_LAYER)) return null;
    const box: [[number, number], [number, number]] = [
      [point.x - 7, point.y - 7],
      [point.x + 7, point.y + 7],
    ];
    const feature = map.queryRenderedFeatures(box, { layers: [RADAR_SITE_LAYER] })[0];
    const id = feature?.properties?.id;
    return typeof id === "string" ? id : null;
  }

  function selectRadarSite(id: string): void {
    view.marks.radar = { ...view.marks.radar, on: true, site: id };
    syncUrl();
    applyRadarView();
  }

  function setRadarProduct(product: "n0b" | "n0g"): void {
    view.marks.radar = { ...view.marks.radar, product };
    syncUrl();
    applyRadarView();
  }

  return {
    syncFillSuppressed,
    stampField,
    renderRadarLegend,
    applyRadarTitle,
    RADAR_REACH_KM,
    loadLiveRadar,
    syncRadarTime,
    applyRadarView,
    syncRiverRamp,
    loadCaseRadar,
    loadCaseFire,
    loadCaseRiver,
    loadCaseContext,
    radarSiteAt,
    selectRadarSite,
    get caseContext() {
      return caseContext;
    },
    get riverCase() {
      return riverCase;
    },
    get radarOverlay() {
      return radarOverlay;
    },
    get radarLevelGroup() {
      return radarLevelGroup;
    },
    get lastRadarReadout() {
      return lastRadarReadout;
    },
    get radarFeed() {
      return radarFeed;
    },
    get caseFireLoaded() {
      return caseFireLoaded;
    },
  };
}
