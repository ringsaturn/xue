// The point probe panel: the pin, the series it reads at one cell across the
// axis, the meteogram rows, the terrain column and the station chips. The
// arithmetic lives in probe.ts and meteogram.ts; this is the controller.
import { type Map as MaplibreMap, Marker } from "maplibre-gl";
import {
  accumulationPlan,
  accumulationVariable,
  planTotal,
  rangePlan,
  type RateKind,
  rateKind,
  runningTotals,
  seriesTotals,
  windowCode,
} from "./accumulation";
import { type AlertHit, type AlertLayers, alertLevelColor } from "./alerts/layer";
import type { LoadedAqIndex } from "./aq/fetch";
import type { AqStation } from "./aq/schema";
import { askingOf, type ColumnSource, readingFrom } from "./columnsource";
import { domainContains, lambertCone, type LambertDomain } from "./domain";
import { formatPointDegrees } from "./format";
import { coordinateFormat, onMapPrefsChange } from "./mapprefs";
import { formatCoordinate } from "./coordinates";
import { shellPath, t } from "./i18n";
import { identifyBundle } from "./identity";
import { COLUMN_LEVELS_HPA, type ColumnLevel, siteTemperature, sortedColumn } from "./lapse";
import { disrpClassLabelKey } from "./levels";
import {
  axisUnitSeconds,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  frameOffsets,
  hasBundle,
} from "./manifest";
import {
  alignSeries,
  type DayMark,
  drawMeteogram,
  frameIndexAtX,
  meteogramRowCode,
  type MeteogramRowData,
  meteogramRows,
  type MeteogramRowSpec,
  seriesState,
  TAF_ROW_SPEC,
  TERRAIN_ROW_SPEC,
  meteogramRowBundles,
} from "./meteogram";
import { decodeValue } from "./palettes";
import {
  bilinearCells,
  geoGrid,
  blendValues,
  normalizeLongitude,
  probeCell,
  ProbeSeries,
  probeSeriesValues,
  type ProbeValue,
  type ProbeVariable,
  probeWindDirection,
} from "./probe";
import type { RiverCase } from "./river/case";
import { createGaugeSection } from "./river/gauge";
import type { TimedSeries } from "./river/hydrograph";
import type { RiverGauge } from "./river/index";
import type { PointSession, VariableSession } from "./session";
import { parseFrameCacheKey } from "./sessionkeys";
import { createSheet } from "./sheet";
import { modelColumn, type ModelColumn } from "./sounding/column";
import {
  modelColumnBundles,
  modelColumnPossible,
  modelColumnSurfaceIds,
  modelProfileBundles,
  nearestFrameForTime,
} from "./sounding/model";
import { type ModelLevel, type Profile, profileFromModel } from "./sounding/profile";
import { createSoundingSection, type SoundingSection } from "./sounding/section";
import {
  fetchAirportStation,
  fetchSynopStation,
  type LoadedAirportIndex,
  type LoadedSoundingIndex,
  type LoadedSynopIndex,
} from "./stations/fetch";
import { categoryColor, pm25Color } from "./stations/layers";
import {
  nearestAirport,
  nearestAq,
  nearestSounding,
  type NearestStation,
  nearestSynopStations,
  synopForElevation,
} from "./stations/nearest";
import {
  nearestObservation,
  type ObservationAxis,
  rowObservations,
  rowObservedValue,
  tafBands,
} from "./stations/observations";
import type { AirportStation, AirportStationHistory, SynopStation, SynopStationSeries } from "./stations/schema";
import { createObservationSection } from "./stations/obssection";
import { createProbeLayout, type ProbeLayout } from "./probelayout";
import { synopObservedValue, synopRowObservations, synopTakesRow } from "./stations/synopobs";
import { demElevationAt } from "./terrain";
import type { TimelinePlan, TimelineSegment } from "./timeline";
import { setPinnedZone, zoneAt, zoneDisplayName } from "./timezone";
import { formatDistance, formatHeight, formatValueReadout, variableDisplayUnit, variableDisplayValue } from "./units";
import { statisticNote } from "./statistics";
import type { ViewState } from "./viewstate";

/** What the probe panel reads off the page. Its own state (the pin, the
 * series, the station chips, the skew-T sheet) lives in the factory; the
 * page's state reaches it here, the mutable parts as getters so every read
 * sees the value at the time, never one captured at creation. */
export interface ProbePanelDeps {
  readonly map: MaplibreMap;
  readonly view: ViewState;
  readonly slider: HTMLInputElement;
  readonly timelinePanel: HTMLElement;

  // The run and the primary session on screen, read live.
  readonly manifest: ForecastManifest | null;
  readonly currentRun: string | null;
  readonly metadata: BundleMetadata | null;
  readonly activeSession: VariableSession | null;
  readonly activeVariable: BundleVariable | null;
  readonly activeFrameIndex: number | null;
  readonly ready: boolean;
  readonly timeline: TimelinePlan;
  /** The run's load sequence: a probe session opened under an older one is
   * dropped when it lands. */
  readonly initializeSequence: number;
  /** The frame-request generation: a scrub on a chart bumps it like a scrub
   * on the track, so stale decodes are ignored. */
  generation: number;
  /** The worker request id counter shared with every other request. */
  nextRequestId: number;

  // The point products a pin resolves against, whichever have loaded.
  readonly soundingLoaded: LoadedSoundingIndex | null;
  readonly airportLoaded: LoadedAirportIndex | null;
  readonly synopLoaded: LoadedSynopIndex | null;
  readonly aqLoaded: LoadedAqIndex | null;
  readonly alertLayers: AlertLayers | null;
  readonly riverCase: RiverCase | null;

  // Sessions and the frame cache.
  readonly sessions: Map<string, VariableSession>;
  readonly columns: ColumnSource;
  readonly planeCache: ReadonlyMap<string, { plane: Uint8Array; session: VariableSession }>;
  readonly slotSessions: () => VariableSession[];
  readonly probeSession: () => VariableSession | null;
  readonly loadVariable: (variableId: ForecastBundleId, sequence: number, role: "probe") => Promise<VariableSession>;
  readonly probeKey: (session: PointSession, variable: BundleVariable) => string;
  readonly probeVariables: (session: PointSession) => ProbeVariable[];
  readonly memberProbeOffsets: (session: VariableSession) => number[];
  readonly sessionOffsetForLead: (session: PointSession, seconds: number) => number | null;
  readonly memberOffsetForFrame: (session: VariableSession, index: number) => number | null;
  readonly accumulationOn: (
    session: VariableSession,
  ) => { span: number; range: { from: number; to: number } | null; kind: RateKind } | null;
  readonly variableUi: (session: VariableSession) => { code: string };
  readonly modelDomain: (metadata: BundleMetadata) => LambertDomain | null;

  // The time axis and the playhead.
  readonly frameCount: () => number;
  readonly frameAxis: () => number[];
  readonly frameLeads: () => number[];
  readonly frameOffset: (index: number) => number;
  readonly frameLeadSeconds: (index: number) => number;
  readonly frameValidTime: (index: number) => number;
  readonly showingObservations: () => boolean;
  readonly probeStampLine: (index: number, member: VariableSession | null) => string;
  readonly segmentLabel: (segment: TimelineSegment) => string;
  readonly formatDate: (value: string | number) => string;
  readonly formatCompactDate: (value: number) => string;
  readonly stopPlayback: () => void;
  readonly trySelectFrame: (index: number) => boolean;
}

/** Where the reader clicked, as they write coordinates: a grid reference to
 * the metre (`coordinates.ts`), or degrees to about ten metres (also where
 * the chosen grid has no reference for the point, as BNG outside Great
 * Britain). This is not where the numbers come from — that is the model
 * cell's centre, shown beside it (`cellCentreText`). */
function pinnedPointText(latitude: number, longitude: number): string {
  const reference = formatCoordinate(coordinateFormat, latitude, longitude, 1);
  return reference ?? `${formatPointDegrees(latitude, "NS", 4)} ${formatPointDegrees(longitude, "EW", 4)}`;
}

/** The centre of the model cell the readings are for, in degrees: a model
 * grid is laid out in degrees (or its own projection), never in a grid
 * reference, and a whole cell has no metre to name. */
function cellCentreText(latitude: number, longitude: number): string {
  return t("probeCell", { position: `${formatPointDegrees(latitude, "NS")} ${formatPointDegrees(longitude, "EW")}` });
}

/** The probe panel's controller: builds the panel, the pin and the skew-T
 * sheet, and returns what the page calls to pin, refresh and close it. */
export function createProbePanel(deps: ProbePanelDeps) {
  const {
    accumulationOn,
    frameLeads,
    map,
    slider,
    timelinePanel,
    sessionOffsetForLead,
    memberOffsetForFrame,
    slotSessions,
    sessions,
    columns,
    planeCache,
    view,
    formatDate,
    formatCompactDate,
    frameCount,
    frameAxis,
    showingObservations,
    frameOffset,
    frameLeadSeconds,
    frameValidTime,
    probeStampLine,
    probeKey,
    probeVariables,
    stopPlayback,
    modelDomain,
    trySelectFrame,
    segmentLabel,
    variableUi,
    loadVariable,
    probeSession,
    memberProbeOffsets,
  } = deps;
  // The point probe: a click pins one grid cell and reads it across the whole
  // time axis. On a container v2 bundle it asks for the series outright — one
  // chunk per temporal group of the one tile holding the cell, a few dozen KB
  // whatever the axis length — so the chart is complete the moment it opens.
  // Where the container cannot address a cell (a v1 bundle) it falls back to sampling every plane the app decodes for the screen
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
  /** The DEM elevation under the pin, and the point it was read for: the sample
   * is a network read, and a moved pin must not wear the old point's number. */
  let probeDem: { key: string; value: number | null } | null = null;
  /** Series requests in flight, so a re-render or a session swap does not ask
   * twice for the same cell. Keyed by `variableId:column:row`. */
  const probeSeriesRequests = new Set<string>();
  /** The series requests a session's worker has yet to answer, by request
   * id — what an `error` reply names — with the key each was asked under,
   * so a read the worker could not make can be asked for again. Kept by
   * session, so a session's entries go with it. */
  const probeSeriesPending = new WeakMap<PointSession, Map<number, string>>();
  /** How often each read has failed for the pin on screen. */
  const probeSeriesFailures = new Map<string, number>();
  /** How many times a failed read is asked for again before the row is
   * left empty for this pin; the wait grows with each. */
  const PROBE_SERIES_RETRIES = 3;
  const PROBE_SERIES_RETRY_MS = 2000;
  /** The four cells around the pin on each grid a bilinear read is taken on,
   * each a series of its own, and their weights — the map's shader filters
   * the 2 m temperature and the orography between cells, so the terrain row
   * reads them the same way (`PROBE_BILINEAR_IDS`). Keyed by the bundle's
   * metadata, dropped with the pin. */
  const probeCorners = new Map<BundleMetadata, { series: ProbeSeries[]; weights: number[] } | null>();
  /** The bundles read bilinearly at the pin, for the terrain row. */
  const PROBE_BILINEAR_IDS: ReadonlySet<string> = new Set(["tmp2m", "orog"]);

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
  /** The surface stations near the pin, nearest first, and the windows read
   * so far by station id. Which of them speaks for the pin waits on the
   * ground height under it (`probeSynop`), so the candidates are kept rather
   * than one chosen at pin time. */
  let probeSynopCandidates: NearestStation<SynopStation>[] = [];
  /** The air-quality station within reach of the pin, from the index; and the
   * highest warning drawn under the pin, while the warnings are on screen. */
  let probeAq: NearestStation<AqStation> | null = null;
  let probeAlert: AlertHit | null = null;
  const probeSynopSeries = new Map<string, SynopStationSeries | "pending" | "failed">();
  let probeSynopSequence = 0;

  /** The day strip and the row pitch of the meteogram, shared by the canvas
   * and the DOM rows beside it so the two stay aligned. A row is two lines of
   * text — the code, then the readout with its unit — and the pitch is what
   * those need. */
  const METEOGRAM_HEADER_HEIGHT = 12;
  const METEOGRAM_ROW_HEIGHT = 30;

  /** How the panel shares the screen with the map (`probelayout.ts`): made
   * once the panel and its sections are, read by them at draw time. */
  let probeLayout: ProbeLayout | null = null;

  /** The skew-T under the rows. Built before the panel, since the panel puts
   * its root in place; it reads the model column back out of main.ts at
   * draw time rather than being handed one. */
  const soundingSection: SoundingSection = createSoundingSection({
    modelProfile: () => modelProfileForSounding(),
    modelColumn: () => modelColumnAtPlayhead(),
    columnPossible: () =>
      deps.manifest !== null &&
      !showingObservations() &&
      modelColumnPossible(deps.manifest.bundles.map((entry) => entry.variable)),
    modelName: () => deps.manifest?.model ?? "",
    formatTime: (time) => formatCompactDate(Date.parse(time)),
    // Read at pin time, never captured: `view.marks.stations` follows the rail.
    wantsOpen: () => view.marks.stations.soundings,
    modelPossible: () =>
      deps.manifest !== null &&
      !showingObservations() &&
      modelProfileBundles(deps.manifest.bundles.map((entry) => entry.variable)).length > 0,
    onChange: () => {
      // A newly opened section, or another ascent selected, wants the run's
      // isobaric levels at the pinned cell.
      ensureProbeSessions();
      syncSkewtSheetTitle();
      probeLayout?.sync();
    },
    chartHeight: () => probeLayout?.chartHeight() ?? 320,
  });

  /** A river case's gauge in the probe panel, under the meteogram rows
   * (`river/gauge.ts`); shown only while a gauge's mark opened the pin. */
  const gaugeSection = createGaugeSection(() => scheduleProbeRender());
  const observationSection = createObservationSection();
  const probePanel = buildProbePanel();
  probeLayout = createProbeLayout({
    map,
    panel: probePanel.root,
    head: probePanel.head,
    close: probePanel.close,
    observation: observationSection,
    sounding: soundingSection,
    pin: () => (probe ? { longitude: probe.longitude, latitude: probe.latitude } : null),
  });

  // The full chart, one of the shell's sheets: the panel's 320 px is a
  // reading, and a sheet is what a forecaster actually works the profile in.
  // It is the section's own body moved into the sheet, not a second chart,
  // so the legend, the readings and the selected ascent come with it. The
  // section owns the trigger, so the press, the focus and the Escape are
  // `sheet.ts`'s as they are for the model and sources sheets.
  const skewtSheet = document.getElementById("skewt-sheet");
  const skewtSheetBody = document.getElementById("skewt-sheet-body");
  const skewtSheetTitle = document.getElementById("skewt-sheet-title");
  const skewtSheetController = skewtSheet
    ? createSheet({
        trigger: soundingSection.expandButton,
        sheet: skewtSheet,
        canOpen: () => soundingSection.isOpen() && skewtSheetBody !== null,
        // The body goes back under the section's header, and the focus with
        // it: the trigger was inside the sheet while it was open.
        onClose: () => {
          soundingSection.leaveSheet();
          soundingSection.expandButton.focus({ preventScroll: true });
        },
      })
    : null;

  /** Name the station in the sheet's heading while the sheet is open; the
   * chart under it is the section's and repaints with the section. */
  function syncSkewtSheetTitle(): void {
    if (!skewtSheetController?.isOpen() || !skewtSheetTitle) return;
    skewtSheetTitle.textContent = soundingSection.headline();
  }

  soundingSection.expandButton.addEventListener("click", () => {
    // After `sheet.ts`'s own listener, so the sheet is already open here.
    if (!skewtSheetController?.isOpen() || !skewtSheetBody) return;
    soundingSection.enterSheet(skewtSheetBody);
    syncSkewtSheetTitle();
  });
  // A resized window changes the room the chart has, in the panel as in the
  // sheet; the section repaints only if its size really changed.
  window.addEventListener("resize", () => soundingSection.render());

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
    // Two more chips of the same make: the nearest air-quality station's
    // PM2.5, and the highest warning in force under the pin while the
    // warnings are drawn. Out of the layout when there is none.
    const aqChip = document.createElement("span");
    aqChip.className = "probe-airport probe-aq";
    aqChip.id = "probe-aq";
    aqChip.hidden = true;
    const alertChip = document.createElement("span");
    alertChip.className = "probe-airport probe-alert";
    alertChip.id = "probe-alert";
    alertChip.hidden = true;
    const value = document.createElement("output");
    value.className = "probe-value";
    value.id = "probe-value";
    const meta = document.createElement("span");
    meta.className = "probe-meta";
    meta.id = "probe-meta";
    const coords = document.createElement("span");
    coords.className = "probe-coords";
    coords.id = "probe-coords";
    // The model cell the readings are for; empty while there is none.
    const cell = document.createElement("span");
    cell.className = "probe-cell";
    cell.id = "probe-cell";
    // The pinned cell's terrain height, from the run's static orography when
    // it ships one; empty — and out of the layout — otherwise.
    const elevation = document.createElement("span");
    elevation.className = "probe-elevation";
    elevation.id = "probe-elevation";
    // The zone the pinned point lies in, which every valid time on screen now
    // reads in; empty until the lookup answers.
    const zone = document.createElement("span");
    zone.className = "probe-zone";
    zone.id = "probe-zone";
    // The way into the comparison page at this very point: every live model
    // at the pinned cell on one clock (compare.html), a link so it opens in
    // a tab of its own beside the map. The href follows the pin.
    const compare = document.createElement("a");
    compare.className = "probe-compare";
    compare.id = "probe-compare";
    compare.target = "_blank";
    compare.rel = "noreferrer";
    compare.textContent = t("compareLink");
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
    codeLine.append(code, airport, aqChip, alertChip);
    headline.append(codeLine, value);
    metaLine.append(meta, coords, cell, elevation, zone, compare, footer, close);
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
    root.append(head, rows, gaugeSection.root, observationSection.root, soundingSection.root);
    timelinePanel.parentElement!.insertBefore(root, timelinePanel);
    return {
      root,
      head,
      code,
      airport,
      airportId,
      airportCategory,
      aqChip,
      alertChip,
      coords,
      cell,
      elevation,
      zone,
      compare,
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
    /** The readout's line, value and note together: what must fit the
     * label column. */
    line: HTMLElement;
  }

  let probeRowElements: ProbeRowElements[] = [];

  /** The rows this run can fill, or none before a manifest is up. */
  function currentMeteogramRows(): MeteogramRowSpec[] {
    const run = deps.manifest;
    return run ? meteogramRows((id) => hasBundle(run, id)) : [];
  }

  function syncProbeRowElements(specs: MeteogramRowSpec[]): void {
    const same =
      probeRowElements.length === specs.length &&
      probeRowElements.every(
        (row, index) => row.spec.id === specs[index]!.id && row.spec.bundles.join() === specs[index]!.bundles.join(),
      );
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
      return { spec, root, code, value, note, line };
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

  /** The comparison page at a point, in the page's language: its own page,
   * keyed by the query the way the viewer is. Root-relative, so a language
   * switch moves it with the page's other links (`i18n.ts`). */
  function compareUrl(latitude: number, longitude: number): string {
    const params = new URLSearchParams({ lat: latitude.toFixed(2), lon: longitude.toFixed(2) });
    return `${shellPath("/compare")}?${params.toString()}`;
  }

  /** The top-right link into the comparison page follows the camera: the
   * map's centre, so the page opens on what is being looked at. */
  const compareLink = document.getElementById("compare-link") as HTMLAnchorElement | null;
  function syncCompareLink(): void {
    if (!compareLink) return;
    const center = map.getCenter();
    compareLink.href = compareUrl(center.lat, normalizeLongitude(center.lng));
  }
  syncCompareLink();

  /** A readout's digits (`units.ts::formatValueReadout`); a class field
   * reads as its class's name. */
  function formatProbeValue(variable: BundleVariable, value: number): string {
    if (identifyBundle([variable])?.identity.family === "disrp") {
      const key = disrpClassLabelKey(value);
      return key === null ? "--" : t(key);
    }
    return formatValueReadout(variable, value);
  }

  /** Pin a point and open its panel. Before a session exists there is no grid
   * to sample against, so a click is simply ignored. */
  function setProbe(longitude: number, latitude: number, gauge: RiverGauge | null = null): void {
    if (!deps.activeSession) return;
    // Off a regional model's own footprint a resampled grid still holds a
    // value — the encoder's edge extension — but no forecast, and a native grid
    // holds nothing; either way there is nothing to pin.
    const metadata = deps.activeSession.metadata;
    const domain = modelDomain(metadata);
    if (
      (domain && !domainContains(domain, lambertCone(domain), longitude, latitude)) ||
      (geoGrid(metadata).projection && !probeCell(metadata, longitude, latitude))
    ) {
      closeProbe();
      return;
    }
    probe = new ProbeSeries(longitude, latitude);
    // A gauge's mark pins the gauge's own point and adds its hydrograph; a
    // click anywhere else drops it.
    gaugeSection.setGauge(gauge ? (deps.riverCase?.loaded ?? null) : null, gauge);
    // The requests tracked for the previous pin say nothing about this cell,
    // and keeping them would suppress the series read when a point is pinned
    // again later.
    probeSeriesRequests.clear();
    columns.supersede();
    probeSeriesFailures.clear();
    probeOpenFailures.clear();
    probeCorners.clear();
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
    // The panel that just opened, or grew, may have come to cover the pin.
    probeLayout?.aim();
  }

  /** The two point products at the pinned point: the nearest ascent for the
   * chart, the nearest airport for the rows. Both are resolved from the
   * indexes already loaded — a product not published, or not polled yet, is
   * simply absent — and neither depends on its rail tile being pressed.
   *
   * The airport's day of reports is one range request, made here rather than
   * at draw time so the rows fill in once rather than per frame. */
  function resolveProbeStations(longitude: number, latitude: number): void {
    const sounding = deps.soundingLoaded ? nearestSounding(deps.soundingLoaded.index, longitude, latitude) : null;
    soundingSection.setStation(deps.soundingLoaded, sounding?.station ?? null, sounding?.distanceKm ?? 0);

    probeSynopCandidates = deps.synopLoaded ? nearestSynopStations(deps.synopLoaded.index, longitude, latitude) : [];
    probeSynopSeries.clear();
    probeSynopSequence += 1;

    probeAq = deps.aqLoaded ? nearestAq(deps.aqLoaded.index, longitude, latitude) : null;
    resolveProbeAlerts();
    // The warning areas under a fresh pin may still be loading their tiles.
    map.once("idle", resolveProbeAlerts);

    const airport = deps.airportLoaded ? nearestAirport(deps.airportLoaded.index, longitude, latitude) : null;
    probeAirport = airport
      ? { station: airport.station, distanceKm: airport.distanceKm, history: null }
      : null;
    const sequence = ++probeAirportSequence;
    if (!airport || !deps.airportLoaded) return;
    const source = deps.airportLoaded;
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

  /** The surface station that speaks for the pin and its window, once read:
   * the nearest candidate at about the pin's ground height, or the nearest
   * outright until that height is known. The window is one range request,
   * asked for the first time a render wants it. */
  function probeSynop(): { station: SynopStation; distanceKm: number; series: SynopStationSeries } | null {
    if (!deps.synopLoaded || probeSynopCandidates.length === 0) return null;
    const ground = probeDem?.value ?? null;
    const chosen = synopForElevation(probeSynopCandidates, ground);
    if (!chosen) return null;
    const known = probeSynopSeries.get(chosen.station.id);
    if (known === undefined) {
      probeSynopSeries.set(chosen.station.id, "pending");
      const sequence = probeSynopSequence;
      void fetchSynopStation(deps.synopLoaded, chosen.station)
        .then((series) => {
          if (sequence !== probeSynopSequence) return;
          probeSynopSeries.set(chosen.station.id, series);
          scheduleProbeRender();
        })
        .catch((error: unknown) => {
          if (sequence === probeSynopSequence) probeSynopSeries.set(chosen.station.id, "failed");
          console.warn(
            `synop: ${chosen.station.id} window not read:`,
            error instanceof Error ? error.message : error,
          );
        });
      return null;
    }
    return typeof known === "string" ? null : { ...chosen, series: known };
  }

  function closeProbe(): void {
    if (!probe) return;
    probe = null;
    probeSeriesRequests.clear();
    columns.supersede();
    probeSeriesFailures.clear();
    probeOpenFailures.clear();
    probeCorners.clear();
    probeAirport = null;
    probeAirportSequence += 1;
    probeSynopCandidates = [];
    probeSynopSeries.clear();
    probeSynopSequence += 1;
    probeAq = null;
    probeAlert = null;
    soundingSection.setStation(null, null, 0);
    gaugeSection.setGauge(null, null);
    observationSection.render(null);
    probeMarker?.remove();
    probePanel.root.hidden = true;
    probeLayout?.sync();
    // Unpinned, the clock is the viewer's own again.
    probePinSequence += 1;
    probeZone = null;
    probeDem = null;
    setPinnedZone(null);
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
    setPinnedZone(zone);
    renderProbe();
  }

  /** What one bundle is read through at the pin (`readingFrom`). */
  function pointSession(id: ForecastBundleId): PointSession | undefined {
    return readingFrom(
      columns.session(id),
      sessions.get(id),
      (session) => slotSessions().includes(session) && holdsFrames(session, frameOffsets(session.metadata.time)),
      // A column arrives whole, so its first frame stands for the rest; a
      // layer's frames are also sampled one by one as they are decoded.
      (column) => holdsFrames(column, [frameOffsets(column.metadata.time)[0]!]),
    );
  }

  /** Whether the pin holds a session's codes on the given frames. */
  function holdsFrames(session: PointSession, offsets: readonly number[]): boolean {
    return session.variables.every((variable) => {
      const key = probeKey(session, variable);
      return offsets.every((offset) => probe?.code(key, offset) !== undefined);
    });
  }

  /** Ask for one bundle's series at the pin, of whatever reads it
   * (`askingOf`); false when nothing is open to ask. */
  function requestBundleSeries(id: ForecastBundleId): boolean {
    const asked = askingOf(columns.session(id), sessions.get(id), (session) => slotSessions().includes(session));
    if (asked === null) return false;
    if ("column" in asked) requestPointSeries(asked.column);
    else requestProbeSeries(asked.session);
    return true;
  }

  /**
   * The bundles a pinned point reads: the meteogram's rows always, and the
   * run's isobaric levels while the sounding section is open — the model
   * column laid over the ascent is the same kind of read as a row, one probe
   * session per bundle, a few dozen kilobytes each. A column shown as the
   * profile itself also reads each level's height and the fields it stands
   * on (`sounding/column.ts`).
   *
   * Nothing is opened for a section that is closed: a pin whose chart
   * stays folded costs exactly what a pin with no chart does. A bundle two
   * readers ask for is listed once.
   */
  function probeBundleIds(): string[] {
    return [...new Set(listedProbeBundleIds())];
  }

  function listedProbeBundleIds(): string[] {
    const ids: string[] = [];
    for (const spec of currentMeteogramRows()) ids.push(...meteogramRowBundles(spec));
    // The run's terrain, so the pin can read its elevation. Published as an
    // ordinary bundle, opened like a row: one extra session for the whole
    // point read, and skipped on a run that ships none.
    if (deps.manifest && hasBundle(deps.manifest, "orog")) ids.push("orog");
    // The terrain-correction pair, once the pin is known to stand above the
    // model ground: below it the terrain row needs nothing but the heights.
    if (probeWantsColumn()) ids.push(...terrainColumnIds());
    if (deps.manifest && soundingSection.isOpen()) {
      const published = deps.manifest.bundles.map((entry) => entry.variable);
      if (soundingSection.wantsColumn()) {
        for (const bundle of modelColumnBundles(published)) ids.push(bundle.id);
        ids.push(...modelColumnSurfaceIds(published));
      } else {
        for (const bundle of modelProfileBundles(published)) ids.push(bundle.id);
      }
    }
    return ids;
  }

  /** How many of a pin's bundles are opened at once. Every session is a
   * worker of its own, and a chart with a model column asks for some forty
   * of them: opened together they are forty workers starting and forty
   * reads of the store's metadata in one burst, on top of the frames the
   * screen is streaming. A few at a time, the rows and then the column from
   * the ground up, the pin fills in as fast and the burst never forms. */
  const PROBE_OPEN_CONCURRENCY = 6;
  /** The bundles waiting to be opened, in the order asked for, and the
   * ones waiting or opening — so a second ask for one of them is a no-op. */
  const probeOpenQueue: { id: string; sequence: number }[] = [];
  const probeOpening = new Set<string>();
  let probeOpenCount = 0;
  /** How often each bundle has failed to open for the pin on screen, by
   * dataset and id. An open reads the array's own metadata over the wire,
   * and a chart with a model column makes some seventy of them: one that
   * the wire fails past the store's retries would otherwise leave its level
   * out of the profile until the next click asked again. */
  const probeOpenFailures = new Map<string, number>();
  /** How many times a failed open is tried again before the bundle is left
   * closed for this pin; the wait grows with each, as a failed read's does. */
  const PROBE_OPEN_RETRIES = 3;
  const PROBE_OPEN_RETRY_MS = 2000;

  /** Open the meteogram's bundles for the pinned point, quietly and without
   * ever downloading one whole: a bundle the origin cannot range-serve is
   * left closed, and its row simply stays empty. A bundle already open for
   * the screen is reused as it is, and one the run's column store serves is
   * not opened: which those are is known once the store is, and a bundle it
   * may hold waits until then. Each session asks for its series the moment it is ready. */
  function ensureProbeSessions(): void {
    if (!probe || !deps.manifest || !deps.ready) return;
    const sequence = deps.initializeSequence;
    for (const id of probeBundleIds()) {
      const awaiting = columns.awaiting(id);
      if (requestBundleSeries(id) || awaiting || probeOpening.has(id)) continue;
      probeOpening.add(id);
      probeOpenQueue.push({ id, sequence });
    }
    openQueuedProbeSessions();
  }

  /** Start as many of the waiting opens as the limit leaves room for. One
   * that is no longer wanted by the time its turn comes — the pin was
   * dropped, the dataset changed, the chart was folded — is let go. */
  function openQueuedProbeSessions(): void {
    const wanted = new Set(probe && deps.manifest ? probeBundleIds() : []);
    while (probeOpenCount < PROBE_OPEN_CONCURRENCY && probeOpenQueue.length > 0) {
      const { id, sequence } = probeOpenQueue.shift()!;
      if (sequence !== deps.initializeSequence || !wanted.has(id)) {
        probeOpening.delete(id);
        continue;
      }
      probeOpenCount += 1;
      void loadVariable(id, sequence, "probe")
        .then((session) => {
          if (sequence !== deps.initializeSequence || !probe) return;
          requestProbeSeries(session);
          scheduleProbeRender();
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          const key = `${sequence}:${id}`;
          const failures = (probeOpenFailures.get(key) ?? 0) + 1;
          probeOpenFailures.set(key, failures);
          // Diagnostics stay English: a row that cannot open is a console
          // note, never an error panel over the map.
          console.warn(
            `meteogram: ${id} not opened (try ${failures}):`,
            error instanceof Error ? error.message : error,
          );
          if (failures > PROBE_OPEN_RETRIES) return;
          const pinned = probe;
          setTimeout(() => {
            // Another point or another dataset since: its own opens are
            // under way. Asking for every wanted bundle again opens the ones
            // that are neither open nor opening, which is this one.
            if (!pinned || probe !== pinned || sequence !== deps.initializeSequence) return;
            ensureProbeSessions();
          }, PROBE_OPEN_RETRY_MS * failures);
        })
        .finally(() => {
          probeOpenCount -= 1;
          probeOpening.delete(id);
          openQueuedProbeSessions();
        });
    }
  }

  /** Ask every probed session for the pinned cell's whole series: the ones on
   * screen and the meteogram's, resident or served by the column store. */
  function requestAllProbeSeries(): void {
    for (const session of slotSessions()) requestProbeSeries(session);
    for (const id of probeBundleIds()) requestBundleSeries(id);
  }

  /** Ask one session's decoder for the pinned cell's whole series, one
   * request per data variable. Only a tiled bundle can answer; everywhere else
   * the opportunistic sampling in `handleDecodedFrame` remains the only source.
   */
  function requestProbeSeries(session: VariableSession): void {
    if (session.tiles) requestPointSeries(session);
  }

  /** The same of anything that answers a series: a tiled session, or a bundle
   * the column store serves. */
  function requestPointSeries(session: PointSession): void {
    if (!probe || !deps.ready) return;
    const cell = probe.cellFor(session.metadata);
    if (!cell) return;
    postProbeSeries(session, cell.column, cell.row);
    if (!PROBE_BILINEAR_IDS.has(session.id)) return;
    for (const corner of cornersFor(session)?.series ?? []) {
      const at = corner.cellFor(session.metadata);
      if (at) postProbeSeries(session, at.column, at.row);
    }
  }

  /** Ask a session's worker for one cell's series of every variable, once. */
  function postProbeSeries(session: PointSession, column: number, row: number): void {
    for (const variable of session.variables) {
      const key = `${probeKey(session, variable)}:${column}:${row}`;
      if (probeSeriesRequests.has(key)) continue;
      probeSeriesRequests.add(key);
      const requestId = deps.nextRequestId++;
      let pending = probeSeriesPending.get(session);
      if (!pending) {
        pending = new Map();
        probeSeriesPending.set(session, pending);
      }
      pending.set(requestId, key);
      session.worker.postMessage({
        type: "series",
        requestId,
        generation: deps.generation,
        variableId: variable.numericId,
        column,
        row,
      });
    }
  }

  /** The pin's four cells on a session's grid (`probeCorners`), or null off
   * the grid. */
  function cornersFor(session: PointSession): { series: ProbeSeries[]; weights: number[] } | null {
    if (!probe) return null;
    if (probeCorners.has(session.metadata)) return probeCorners.get(session.metadata)!;
    const around = bilinearCells(session.metadata, probe.longitude, probe.latitude);
    const corners = around
      ? { series: around.cells.map((cell) => new ProbeSeries(cell.longitude, cell.latitude)), weights: around.weights }
      : null;
    probeCorners.set(session.metadata, corners);
    return corners;
  }

  /** One bundle's series at the pin on the primary axis, bilinear between the
   * four cells around it; undefined frames until all four have arrived. */
  function probeBilinearSeries(session: PointSession | undefined, leads: readonly number[]): ProbeValue[] | null {
    const corners = session ? cornersFor(session) : null;
    if (!session || !corners) return null;
    const read = corners.series.map((corner) => probeSessionSeries(corner, session, leads).values);
    return leads.map((_, index) => blendValues(read.map((values) => values[index]), corners.weights));
  }

  /** A whole series arrived. It is dropped unless the pin is still on the very
   * cell it was read for: a moved pin makes it stale, and the codes are
   * meaningless against another cell. A new run or model needs no check
   * here — its sessions are other sessions, and a series is filed under the
   * session it came from. The generation the request carried is not asked
   * about: it moves with every step of the transport, and a series is the
   * whole axis, as true after a seek as before it. Dropping a reply that a
   * seek overtook left its level out of the chart for as long as the pin
   * stood, since a read is asked for once. */
  function handleProbeSeries(
    session: PointSession,
    message: {
      requestId?: number;
      generation: number;
      variableId: number;
      column: number;
      row: number;
      buffer: ArrayBuffer;
    },
  ): void {
    if (message.requestId !== undefined) probeSeriesPending.get(session)?.delete(message.requestId);
    const variable = session.variables.find((item) => item.numericId === message.variableId);
    if (!probe || !variable) return;
    const offsets = frameOffsets(session.metadata.time);
    const codes = new Uint8Array(message.buffer);
    const key = probeKey(session, variable);
    let adopted = false;
    // The cell is the pin's own, or one of the four a bilinear read takes —
    // the nearest is usually one of them too.
    for (const series of [probe, ...(probeCorners.get(session.metadata)?.series ?? [])]) {
      const cell = series.cellFor(session.metadata);
      if (!cell || cell.column !== message.column || cell.row !== message.row) continue;
      if (series.adopt(session.metadata, key, offsets, codes)) adopted = true;
    }
    if (adopted) scheduleProbeRender();
  }

  /**
   * A series read the worker could not make: the wire failed it past the
   * store's own retries. The rows are the pinned point's context and never
   * worth a dialog over the map, so the read is asked for again, a few
   * times and after a growing wait, and then left: the row stays empty for
   * this pin. True when the failed request was one of the panel's, which
   * is what tells the caller not to treat it as a frame's.
   */
  function probeSeriesFailed(session: VariableSession, requestId: unknown, reason: string): boolean {
    if (typeof requestId !== "number") return false;
    const pending = probeSeriesPending.get(session);
    const key = pending?.get(requestId);
    if (pending === undefined || key === undefined) return false;
    pending.delete(requestId);
    const failures = (probeSeriesFailures.get(key) ?? 0) + 1;
    probeSeriesFailures.set(key, failures);
    // Diagnostics stay English and stay in the console.
    console.warn(`probe: ${session.id} series not read (try ${failures}):`, reason);
    if (failures > PROBE_SERIES_RETRIES) return true;
    const pinned = probe;
    setTimeout(() => {
      // Another point has been pinned since: its own reads are under way.
      if (!pinned || probe !== pinned) return;
      probeSeriesRequests.delete(key);
      requestProbeSeries(session);
    }, PROBE_SERIES_RETRY_MS * failures);
    return true;
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

  /** The three guns of a composite at one frame as the panel's value line —
   * `R .62 G .40 B .71`, each in [0, 1] — or null until all three are
   * sampled or where any is no data. Instrument text, English in every
   * locale. */
  function compositeProbeReadout(series: ProbeSeries, variables: readonly ProbeVariable[], offset: number): string | null {
    if (variables.length < 3) return null;
    const parts: string[] = [];
    for (const [at, { key, variable }] of variables.slice(0, 3).entries()) {
      const code = series.code(key, offset);
      if (code === undefined) return null;
      const value = decodeValue(variable, code);
      if (value === null || value < 0) return null;
      parts.push(`${"RGB"[at]} ${Math.min(1, value).toFixed(2).replace(/^0/, "")}`);
    }
    return parts.join(" ");
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

  /** The pinned cell's terrain, from two sources: the run's static `orog`
   * bundle where it publishes one, read at that bundle's own grid cell, and the
   * DEM under the point that was pinned — not the cell's centre, which on a
   * quarter-degree grid can sit 15 km away down a mountain's flank — sampled
   * from the very relief the map draws. Either can be absent — a run with no orography, a tile that does not
   * answer — and the line carries whichever arrived. */
  function renderProbeElevation(point: { latitude: number; longitude: number }): void {
    probePanel.elevation.textContent = [modelElevationText(), demElevationText()].filter(Boolean).join(" · ");
    // The DEM is a fetch, so it lands a frame or two after the model's own
    // number; ask for the point on screen, and drop the answer if the pin has
    // moved on by the time it arrives.
    const key = `${point.latitude.toFixed(4)},${point.longitude.toFixed(4)}`;
    if (probeDem?.key === key) return;
    probeDem = { key, value: null };
    void demElevationAt(point.latitude, point.longitude).then((value) => {
      if (probeDem?.key !== key) return;
      probeDem = { key, value };
      renderProbeElevation(point);
      // The terrain row waits on this height.
      scheduleProbeRender();
    });
  }

  /** The model's own terrain height in metres at the pin, or null. Read at the
   * orog bundle's own grid cell — the primary's on every source that carries
   * terrain. */
  function modelElevationValue(): number | null {
    const session = pointSession("orog");
    if (!probe || !session) return null;
    if (!probe.cellFor(session.metadata)) return null;
    const value = probeSeriesValues(probe, probeVariables(session), frameOffsets(session.metadata.time))[0];
    return typeof value === "number" ? value : null;
  }

  /** The model's ground height under the pin as the map's shader reads it,
   * bilinear between the four cells around it; null until all four are in.
   * The readout keeps the nearest cell's (`modelElevationValue`): the
   * model's own number. */
  function modelElevationBilinear(): number | null {
    const session = pointSession("orog");
    if (!session) return null;
    // A static bundle: its one frame, at its own first offset.
    const time = session.metadata.time;
    const lead = (frameOffsets(time)[0] ?? 0) * axisUnitSeconds(time);
    const value = probeBilinearSeries(session, [lead])?.[0];
    return typeof value === "number" ? value : null;
  }

  /** The pin's two heights, the model ground's and the DEM's, once both are
   * in: what the terrain row carries the 2 m temperature between. */
  function probeTerrainHeights(): { model: number; site: number } | null {
    const model = modelElevationBilinear();
    const site = probeDem?.value;
    if (model === null || site === null || site === undefined) return null;
    return { model, site };
  }

  /** The isobaric surfaces the run publishes both the temperature and the
   * height of, among those the column is read on (lapse.ts), bottom first —
   * none on a run that has fewer than two. */
  function terrainColumnLevels(): { level: number; temperature: string; height: string }[] {
    if (!deps.manifest) return [];
    const levels = COLUMN_LEVELS_HPA.filter((level) => hasBundle(deps.manifest!, `tmp${level}`) && hasBundle(deps.manifest!, `hgt${level}`)).map(
      (level) => ({ level, temperature: `tmp${level}`, height: `hgt${level}` }),
    );
    return levels.length >= 2 ? levels : [];
  }

  /** The bundles of the column, temperature and height level by level. */
  function terrainColumnIds(): string[] {
    return terrainColumnLevels().flatMap((level) => [level.temperature, level.height]);
  }

  /** Whether the pin stands above the model ground of a run with a column,
   * so the terrain row reads it. */
  function probeWantsColumn(): boolean {
    const heights = probeTerrainHeights();
    return heights !== null && heights.site > heights.model && terrainColumnLevels().length > 0;
  }

  /** The pin and run a terrain-row fallback was last reported for. */
  let terrainFallbackNoted: string | null = null;

  /** The terrain row's series: the model's 2 m temperature carried to the
   * DEM's height under the pin frame by frame, through the run's isobaric
   * column above the model ground and at the standard lapse rate below it. A
   * frame whose column is incomplete takes the standard rate too, said once
   * on the console rather than on the row. */
  function probeTerrainSeries(
    temperature: readonly ProbeValue[],
    heights: { model: number; site: number },
    series: ProbeSeries,
    leads: readonly number[],
  ): ProbeValue[] {
    const wanted = probeWantsColumn();
    const levels = wanted
      ? terrainColumnLevels().map((level) => ({
          temperature: probeSessionSeries(series, pointSession(level.temperature), leads),
          height: probeSessionSeries(series, pointSession(level.height), leads),
        }))
      : [];
    // The map's shader filters the 2 m temperature between cells; the row
    // takes the same blend once the four cells are in, the nearest until then.
    const blended = probeBilinearSeries(pointSession("tmp2m"), leads);
    let fallbacks = 0;
    const values = temperature.map((nearest, index) => {
      const between = blended?.[index];
      const t2m = typeof between === "number" ? between : nearest;
      if (typeof t2m !== "number") return t2m;
      const column: ColumnLevel[] = [];
      for (const level of levels) {
        const t = level.temperature.values[index];
        const z = level.height.values[index];
        if (typeof t === "number" && typeof z === "number") column.push({ height: z, temperature: t });
      }
      const carried = siteTemperature(t2m, heights.model, heights.site, wanted ? sortedColumn(column) : null);
      if (wanted && carried.fallback) fallbacks += 1;
      return carried.value;
    });
    // Reported only once the column is read through: while it loads, every
    // frame is a fallback for a moment and none of it is worth a line.
    const read = levels.length > 0 && levels.every((level) => level.temperature.state === "complete" && level.height.state === "complete");
    const key = `${probeDem?.key ?? ""}:${deps.currentRun ?? ""}`;
    if (fallbacks > 0 && read && terrainFallbackNoted !== key) {
      terrainFallbackNoted = key;
      // Diagnostics stay English.
      console.info(`terrain row: ${fallbacks} of ${temperature.length} frames used the fixed lapse rate (column incomplete)`);
    }
    return values;
  }

  /** The model's own terrain height, from the `orog` bundle, else empty. */
  function modelElevationText(): string {
    const session = pointSession("orog");
    const value = modelElevationValue();
    if (!session || value === null) return "";
    const unit = variableDisplayUnit(session.variable);
    return `${t("probeModelElevationLabel")} ${formatProbeValue(session.variable, variableDisplayValue(session.variable, value))} ${unit}`.trimEnd();
  }

  /** The DEM's own elevation under the pin, to whole metres: the samples carry a
   * 1/256 m fraction the terrain does not resolve. Empty until it arrives. */
  function demElevationText(): string {
    const value = probeDem?.value;
    if (value === null || value === undefined) return "";
    return `${t("probeDemLabel")} ${formatHeight(value)}`;
  }

  // The coordinate format is the reader's; the pin's readout follows it.
  onMapPrefsChange(() => renderProbe());

  function renderProbe(): void {
    const series = probe;
    if (!series) return;
    // On a mosaic the pin reads the member whose band it sits in; its axis is
    // another run's, so its frames are found by the playhead's valid time.
    const session = probeSession();
    const member = session !== null && session !== deps.activeSession;
    if (!session) {
      // Between datasets (a model switch, a new run) there is nothing to read:
      // blank the panel rather than leave the previous dataset's numbers up.
      probePanel.value.value = "--";
      probePanel.meta.textContent = t("probeAwaiting");
      probePanel.elevation.textContent = "";
      probeDem = null;
      probePanel.count.textContent = "";
      probePanel.hint.textContent = "";
      probePanel.canvas.getContext("2d")?.clearRect(0, 0, probePanel.canvas.width, probePanel.canvas.height);
      syncProbeRowElements([]);
      renderProbeAirportChip();
      renderProbeObservations();
      soundingSection.render();
      return;
    }
    const cell = series.cellFor(session.metadata);
    probePanel.code.textContent = variableUi(session).code;
    renderProbeAirportChip();
    const point = cell ?? { longitude: series.longitude, latitude: series.latitude };
    probePanel.coords.textContent = pinnedPointText(series.latitude, series.longitude);
    probePanel.cell.textContent = cell ? cellCentreText(cell.latitude, cell.longitude) : "";
    renderProbeElevation({ latitude: series.latitude, longitude: series.longitude });
    probePanel.zone.textContent = probeZone ? zoneDisplayName(probeZone, frameValidTime(deps.activeFrameIndex ?? Number(slider.value))) : "";
    probePanel.compare.href = compareUrl(point.latitude, point.longitude);
    probePanel.compare.textContent = t("compareLink");

    const index = deps.activeFrameIndex ?? Number(slider.value);
    const offsets = member ? memberProbeOffsets(session) : frameAxis();
    const shownOffset = member ? (memberOffsetForFrame(session, index) ?? -1) : frameOffset(index);
    const probed = probeVariables(session);
    // A composite's guns are unitless stretches with no magnitude between
    // them: the readout names the three as they are and draws no trace.
    const rates = cell ? (session.composite ? [] : probeSeriesValues(series, probed, offsets)) : [];
    // A total reads the same series summed over its window at every frame.
    // A range is one total, whatever the playhead: the readout says it and
    // the chart keeps the rate it is summed from.
    const total = member ? null : accumulationOn(session);
    const range = total?.range ?? null;
    const values = total && !range ? seriesTotals(rates, frameLeads(), total.span, total.kind) : rates;
    const variable = total && !range ? accumulationVariable(session.variable) : session.variable;
    const readoutVariable = total ? accumulationVariable(session.variable) : session.variable;
    if (total) probePanel.code.textContent = `APCP ${windowCode(total.span)}`;
    const current = total && range ? planTotal(rates, rangePlan(frameLeads(), range.from, range.to, total.kind)) : values[index];

    if (!cell) {
      probePanel.value.value = "--";
      probePanel.meta.textContent = t("probeOutside");
    } else if (session.composite) {
      probePanel.value.value = compositeProbeReadout(series, probed, shownOffset) ?? "--";
      probePanel.meta.textContent = probeStampLine(index, member ? session : null);
    } else {
      probePanel.value.value =
        typeof current === "number"
          ? `${formatProbeValue(readoutVariable, variableDisplayValue(readoutVariable, current))} ${variableDisplayUnit(readoutVariable)}`.trimEnd()
          : "--";
      const lead = probeStampLine(index, member ? session : null);
      if (current === undefined) probePanel.meta.textContent = `${lead} · ${t("probeAwaiting")}`;
      else if (current === null) probePanel.meta.textContent = `${lead} · ${t("probeNoData")}`;
      else {
        // Wind's series is the speed; the direction only means anything for the
        // frame on screen, so it rides the lead-time line.
        const direction = session.vector ? probeWindDirection(series, probed, shownOffset) : null;
        probePanel.meta.textContent = direction === null
          ? lead
          : `${lead} · ${String(Math.round(direction)).padStart(3, "0")}°`;
        // A window the axis cannot fill yet says how much of it the total is.
        const covered = total && !range ? accumulationPlan(frameLeads(), index, total.span, total.kind).covered : null;
        if (total && covered !== null && covered < total.span) {
          probePanel.meta.textContent += ` · ${windowCode(covered)}/${windowCode(total.span)}`;
        }
        // Which statistic of an ensemble the reading is: a 75th percentile
        // or a spread is not the quantity a single forecast would read. A
        // probability's threshold is a precipitation total, kg m⁻², read in
        // millimetres of water.
        const statistic = session.identity?.statistic;
        const note = total ? null : statisticNote(statistic, session.identity?.family === "pop" ? "mm" : "");
        if (note) probePanel.meta.textContent += ` · ${note}`;
      }
    }

    const sampled = values.reduce<number>((total, value) => total + (value === undefined ? 0 : 1), 0);
    probePanel.count.textContent = cell && !session.composite ? `${sampled} / ${offsets.length}` : "";
    probePanel.hint.textContent = !cell || session.composite ? "" : sampled >= offsets.length ? t("probeComplete") : t("probeHint");
    drawProbeChart(values, index, variable);
    renderProbeRows(series, index);
    renderProbeGauge(series, index);
    renderProbeObservations();
    // The ascent under the rows: its own time, never the playhead's, but the
    // model column laid over it comes from the series just read.
    soundingSection.render();
    syncSkewtSheetTitle();
    probeLayout?.sync();
  }

  /** The warning drawn under the pin, highest level first: read off the
   * rendered areas, so only while the warnings are on screen. */
  function resolveProbeAlerts(): void {
    const series = probe;
    const hits = series && deps.alertLayers ? deps.alertLayers.alertsAt(map.project([series.longitude, series.latitude])) : [];
    probeAlert = hits[0] ?? null;
    renderProbeMarkChips();
  }

  /** The air-quality and warning chips beside the airport's. A station's
   * id and the PM2.5 value are instrument text; the event is the service's
   * own words. */
  function renderProbeMarkChips(): void {
    const darkGround = document.body.dataset.ground === "dark";
    const aq = probeAq;
    const pm25 = aq?.station.values.pm25 ?? null;
    const aqChip = probePanel.aqChip;
    aqChip.hidden = pm25 === null;
    if (aq && pm25 !== null) {
      const value = document.createElement("span");
      value.className = "probe-airport-category";
      value.textContent = `PM2.5 ${pm25.toFixed(1)}`;
      aqChip.style.setProperty("--card-ink", pm25Color(pm25) ?? "");
      aqChip.replaceChildren(value);
      aqChip.title = t("aqNearest", { distance: formatDistance(aq.distanceKm, 0) });
    }
    const alert = probeAlert;
    const alertChip = probePanel.alertChip;
    alertChip.hidden = alert === null;
    if (alert) {
      const level = document.createElement("span");
      level.className = "probe-airport-category";
      level.textContent = `L${alert.line.level}`;
      const event = document.createElement("span");
      event.textContent = alert.line.native.event;
      alertChip.style.setProperty("--card-ink", alertLevelColor(alert.line.level, darkGround));
      alertChip.replaceChildren(level, event);
      alertChip.title = `${t("alertsLevel", { level: alert.line.level })} · ${alert.line.native.event}`;
    }
  }

  /** The nearest airport beside the panel's code: its ICAO id and the flight
   * category in the category's own color, the chip the station card uses. An
   * id and a category are instrument text and are never translated. */
  function renderProbeAirportChip(): void {
    renderProbeMarkChips();
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
    if (!deps.metadata || leads.length === 0) return null;
    return {
      runTimeMs: Date.parse(deps.metadata.runTime),
      firstLead: leads[0]!,
      lastLead: leads[leads.length - 1]!,
    };
  }

  /** One bundle's series at the pinned point on the *primary* axis: a lead
   * time the bundle's own axis lacks is a gap, and a session not open yet
   * (or a point off its grid) is a series of nothing. */
  function probeSessionSeries(
    series: ProbeSeries,
    session: PointSession | undefined,
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
   * is the forecast *for that hour*, whichever frame that is. Past
   * `MODEL_PROFILE_TOLERANCE_MS` there is no frame near enough and the
   * chart draws the sonde alone, with the legend saying so.
   *
   * Every level comes out of the probe series the sessions have already read
   * at the pinned cell — the same one round trip per bundle the rows use — so
   * the column costs nothing beyond those sessions. A wind bundle is read as
   * its two components rather than as the magnitude the rows draw: a profile
   * wants the barb, which needs the direction.
   */
  function modelProfileForSounding(): { profile: Profile; run: string; validTime: number } | null {
    const series = probe;
    if (!series || !deps.manifest || !deps.metadata) return null;
    const time = soundingSection.selectedTime();
    if (time === null) return null;
    const offsets = frameAxis();
    const index = nearestFrameForTime(
      offsets.map((_, position) => frameValidTime(position)),
      Date.parse(time),
    );
    if (index === null) return null;
    const column = modelLevelsAtLead(
      series,
      frameLeadSeconds(index),
      modelProfileBundles(deps.manifest.bundles.map((entry) => entry.variable)),
    );
    if (column.length === 0) return null;
    return { profile: profileFromModel(column), run: modelRunLabel(), validTime: frameValidTime(index) };
  }

  /** The run on screen, as the chart's legend names it. */
  function modelRunLabel(): string {
    if (!deps.manifest) return "";
    return deps.currentRun ? `${deps.manifest.model} ${deps.currentRun}` : deps.manifest.model;
  }

  /** A run's isobaric levels at the pinned cell and one lead time, out of
   * the series the probe sessions have read: a level takes whatever of its
   * temperature, humidity, wind and height has arrived, and one with
   * neither a temperature nor a wind yet is left out. */
  function modelLevelsAtLead(
    series: ProbeSeries,
    lead: number,
    bundles: ReturnType<typeof modelColumnBundles>,
  ): ModelLevel[] {
    const levels = new Map<number, ModelLevel>();
    for (const bundle of bundles) {
      const session = pointSession(bundle.id);
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
          else if (bundle.field === "rh") level.rh = value;
          else level.z = value;
        }
      }
      levels.set(bundle.level, level);
    }
    return [...levels.values()].filter((level) => level.t !== null || level.u !== undefined);
  }

  /**
   * The model column at the playhead, for a chart with no ascent under it.
   *
   * Unlike the column laid over an ascent this one is the forecast for the
   * frame on screen, so it moves with the transport: every series it reads
   * is the whole axis at the pinned cell, and another frame is another
   * column out of the same memory. Null until the levels have begun to
   * arrive.
   */
  function modelColumnAtPlayhead(): {
    column: ModelColumn;
    run: string;
    stamp: string;
    frame: number;
    cape: number | null;
    cin: number | null;
  } | null {
    const series = probe;
    if (!series || !deps.manifest || !deps.metadata) return null;
    const index = deps.activeFrameIndex ?? Number(slider.value);
    const lead = frameLeadSeconds(index);
    const levels = modelLevelsAtLead(
      series,
      lead,
      modelColumnBundles(deps.manifest.bundles.map((entry) => entry.variable)),
    );
    if (levels.length === 0) return null;
    /** One surface bundle's value at this lead, or its two components. */
    const read = (id: string): number[] | null => {
      const session = pointSession(id);
      if (!session || !series.cellFor(session.metadata)) return null;
      const offset = sessionOffsetForLead(session, lead);
      if (offset === null) return null;
      const values = probeVariables(session).map((variable) => probeSeriesValues(series, [variable], [offset])[0]);
      return values.every((value): value is number => typeof value === "number") ? values : null;
    };
    const scalar = (id: string): number | null => read(id)?.[0] ?? null;
    const wind = read("wind10m");
    const column = modelColumn(levels, {
      t: scalar("tmp2m"),
      td: scalar("dpt2m"),
      u: wind && wind.length >= 2 ? wind[0]! : null,
      v: wind && wind.length >= 2 ? wind[1]! : null,
      prmsl: scalar("prmsl"),
      // The terrain is one frame for the whole run, read at its own offset.
      orog: modelElevationValue(),
    });
    if (column.profile.n === 0) return null;
    return {
      column,
      run: modelRunLabel(),
      stamp: probeStampLine(index, null),
      frame: index,
      cape: scalar("cape"),
      cin: scalar("cin"),
    };
  }

  /** The row's readout at the playhead: every series' value in row order
   * ("23.5 · 18.0"), and the unit they share for the line beneath. */
  function formatRowReadout(row: MeteogramRowData, index: number): { values: string; unit: string } {
    const parts: string[] = [];
    let unit = "";
    for (const [position, id] of row.spec.bundles.entries()) {
      const session = pointSession(id);
      if (session) unit ||= variableDisplayUnit(session.variable);
      const value = row.series[position]![index];
      parts.push(
        session && typeof value === "number"
          ? formatProbeValue(session.variable, variableDisplayValue(session.variable, value))
          : "--",
      );
    }
    // The precipitation's running total follows its rate, in its own unit.
    const rate = row.total ? pointSession(row.spec.bundles[0]!) : undefined;
    if (rate && row.total) {
      const depth = accumulationVariable(rate.variable);
      const value = row.total[index];
      parts.push(typeof value === "number" ? formatProbeValue(depth, variableDisplayValue(depth, value)) : "--");
      unit = `${unit} · ${variableDisplayUnit(depth)}`;
    }
    return { values: parts.join(" · "), unit };
  }

  /**
   * The precipitation row's total on the primary axis, following the
   * timeline's window while the precipitation is the field and a total is
   * on: at every frame the window ending there (`?accum=24h`), or across a
   * range, the depth since its first frame and nothing outside it. Null with
   * no total on. It is summed over the rate's own frames, not the primary's:
   * a column the rate's axis lacks (a forecast's analysis hour) is a gap in
   * the line, not a frame the sum waits for.
   */
  function precipitationTotal(
    rates: readonly ProbeValue[],
    leads: readonly number[],
  ): { values: ProbeValue[]; code: string } | null {
    const session = pointSession("prate");
    const on = deps.activeSession?.id === "prate" ? accumulationOn(deps.activeSession) : null;
    if (!session || !on) return null;
    const kind = rateKind(session.variable);
    if (kind === null) return null;
    const present = leads.flatMap((lead, position) => (sessionOffsetForLead(session, lead) === null ? [] : [position]));
    if (present.length === 0) return null;
    const values = present.map((position) => rates[position]);
    const presentLeads = present.map((position) => leads[position]!);
    let totals: ProbeValue[];
    if (on.range) {
      const from = present.findIndex((position) => position >= on.range!.from);
      let to = present.length - 1;
      while (to >= 0 && present[to]! > on.range.to) to -= 1;
      if (from < 0 || to < from) return null;
      totals = runningTotals(values, presentLeads, kind, { from, to });
    } else {
      totals = seriesTotals(values, presentLeads, on.span, kind);
    }
    const out: ProbeValue[] = leads.map(() => null);
    for (const [compact, position] of present.entries()) out[position] = totals[compact];
    return { values: out, code: `APCP ${windowCode(on.span)}` };
  }

  /** The day marks, read off the same plan the capsule's strip is built from
   * — whole forecast days from the run, every other one past six days, month
   * boundaries on a seasonal axis — so the two read the same instants. Every
   * segment is drawn here, including the ones the capsule drops for want of
   * room: the header has the whole width to itself. */
  function meteogramDayMarks(): DayMark[] {
    return deps.timeline.segments.map((segment) => ({ index: segment.index, label: segmentLabel(segment) }));
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
    // The terrain row, under the model's temperature, once the pin has both
    // heights it carries the temperature between.
    const terrainHeights = probeTerrainHeights();
    // The column is read only once both heights put the pin above the model
    // ground, which is known here first. Its sessions may already be open for
    // the map's correction, which reads planes, not the pin's series.
    if (probeWantsColumn()) {
      const ids = terrainColumnIds();
      if (!ids.map(requestBundleSeries).every(Boolean)) ensureProbeSessions();
    }
    const temperatureRow = specs.findIndex((spec) => spec.id === "temperature");
    if (terrainHeights !== null && temperatureRow >= 0 && specs[temperatureRow]!.bundles[0] === "tmp2m") {
      specs.splice(temperatureRow + 1, 0, TERRAIN_ROW_SPEC);
    }
    syncProbeRowElements(specs);
    // The sparkline repeats a row when the field on screen is one of the
    // rows' bundles; then the row stands for it, marked, and the sparkline
    // gives its height back to the map.
    const active = deps.activeSession?.id;
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
    // A surface station at the pin's own height takes the airport's place on
    // the rows both fill; the airport keeps the cloud and its forecast.
    const station = axis ? probeSynop() : null;
    const hasTerrainRow = specs.some((spec) => spec.id === "terrain");
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
      const read = spec.bundles.map((id) => probeSessionSeries(series, pointSession(id), leads));
      const row: MeteogramRowData = {
        spec,
        series: read.map((item) =>
          spec.id === "terrain" && terrainHeights !== null
            ? probeTerrainSeries(item.values, terrainHeights, series, leads)
            : item.values,
        ),
      };
      if (spec.quartiles) {
        const [low, high] = spec.quartiles.map((id) => probeSessionSeries(series, pointSession(id), leads).values);
        row.quartiles = [low!, high!];
      }
      const total = spec.id === "precipitation" ? precipitationTotal(row.series[0]!, leads) : null;
      if (total) row.total = total.values;
      const fromStation = station !== null && synopTakesRow(spec.id, hasTerrainRow);
      if (fromStation && axis) {
        const marks = synopRowObservations(spec.id, station.series, axis);
        if (marks.length) row.observations = marks;
      } else if (history && axis && spec.id !== "terrain") {
        const marks = rowObservations(spec.id, history.metars, axis);
        if (marks.length) row.observations = marks;
      }
      let direction: number | null = null;
      const wind = spec.id === "wind" ? pointSession(spec.bundles[0]!) : undefined;
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
      const observed = fromStation
        ? synopObservedValue(spec.id, station.series, frameValidTime(index))
        : observedNow === null || spec.id === "terrain"
          ? null
          : rowObservedValue(spec.id, observedNow);
      const session = pointSession(spec.bundles[0]!);
      const reported =
        observed === null || !session
          ? null
          : `${t("soundingObserved")} ${formatProbeValue(session.variable, variableDisplayValue(session.variable, observed))}`;
      // The label column is the capsule's and cannot grow, so the line holds
      // two things at most. Where an observation is in hand it takes the
      // place of both the unit and the wind's direction: the unit moves up
      // to the code line for that row, and the direction is already drawn
      // as an arrow under every column of the row. The measurement is
      // nowhere else, so it is what the line keeps.
      const code = total ? `${meteogramRowCode(spec)} · ${total.code}` : meteogramRowCode(spec);
      element.code.textContent = reported !== null && readout.unit ? `${code} · ${readout.unit}` : code;
      const parts = [
        reported !== null ? null : readout.unit,
        reported !== null || direction === null
          ? reported
          : `${String(Math.round(direction)).padStart(3, "0")}°`,
      ].filter((part): part is string => part !== null && part !== "");
      element.note.textContent = parts.join(" · ");
      // Two readouts, a unit and the wind's direction can outrun the label
      // column (the wind's speed and gust with both; PM2.5 · PM10 at a few
      // hundred µg/m³ on a phone). The direction goes first, since the
      // chart draws it as an arrow under every column; then the unit, which
      // the headline carries and the code implies.
      if (reported === null) {
        const keep = [...parts];
        for (const drop of [direction === null ? null : keep[keep.length - 1], readout.unit]) {
          if (element.line.scrollWidth <= element.line.clientWidth) break;
          if (!drop || !keep.includes(drop)) continue;
          keep.splice(keep.indexOf(drop), 1);
          element.note.textContent = keep.join(" · ");
        }
      }
    }
    drawProbeRowsChart(rows, index, offsets.length);
  }

  /** The pinned gauge's hydrograph: its observed line (the section reads it),
   * and the model at it — the track's column on a network case, the case's
   * own discharge field at the pin on a grid one. */
  /** The station whose marks the rows carry, its whole window as charts:
   * the rows reach back only to the run's first frame. */
  function renderProbeObservations(): void {
    const chosen = probe ? probeSynop() : null;
    const network = chosen ? (deps.synopLoaded?.index.networks[chosen.station.network] ?? null) : null;
    observationSection.render(chosen ? { ...chosen, network } : null);
  }

  function renderProbeGauge(series: ProbeSeries, index: number): void {
    const gauge = gaugeSection.gauge();
    if (!gauge || !deps.metadata) return;
    const leads = frameAxis().map((_, position) => frameLeadSeconds(position));
    const runTimeMs = frameValidTime(0) - (leads[0] ?? 0) * 1000;
    let modelled: TimedSeries | null = null;
    if (gauge.feature !== undefined) {
      modelled = deps.riverCase?.modelSeries(gauge) ?? null;
    } else {
      const discharge = sessions.get("dis06");
      if (discharge) {
        const read = probeSessionSeries(series, discharge, leads);
        modelled = {
          times: leads.map((lead) => runTimeMs + lead * 1000),
          values: read.values.map((value) => (typeof value === "number" ? value : null)),
        };
      }
    }
    const styles = getComputedStyle(document.body);
    gaugeSection.render({
      runTimeMs,
      leadSeconds: leads,
      selected: index,
      time: frameValidTime(index),
      modelled,
      formatTime: (time) => formatDate(time),
      ink: {
        ink: styles.getPropertyValue("--accent").trim() || "#54d6c7",
        muted: styles.getPropertyValue("--muted").trim() || "#8ca6b2",
      },
    });
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
      if (!deps.activeVariable) return;
      const rect = canvas.getBoundingClientRect();
      const width = rect.width - gutter;
      const index = frameIndexAtX(event.clientX - rect.left - gutter, frameCount(), width);
      stopPlayback();
      deps.generation += 1;
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
  bindProbeScrub(gaugeSection.canvas);


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
    context.fillText(formatProbeValue(variable, variableDisplayValue(variable, highest)), gutter - 8, top);
    if (!flat) context.fillText(formatProbeValue(variable, variableDisplayValue(variable, lowest)), gutter - 8, bottom);
  }

  // A bundle the store stops serving is opened like any other, at once: a
  // level left out would stay out for as long as the pin stands.
  columns.listen({ series: handleProbeSeries, changed: ensureProbeSessions });

  return {
    scheduleProbeRender,
    handleProbeSeries,
    probeSeriesFailed,
    resolveProbeAlerts,
    setProbe,
    soundingSection,
    seedProbeFromCache,
    requestAllProbeSeries,
    ensureProbeSessions,
    terrainColumnLevels,
    requestProbeSeries,
    probeSeriesRequests,
    probeCorners,
    probePanel,
    renderProbe,
    closeProbe,
    syncCompareLink,
    get probeZone() {
      return probeZone;
    },
    get probe() {
      return probe;
    },
    get skewtSheetTookEscape() {
      return skewtSheetTookEscape;
    },
  };
}

/** What the page holds of the probe panel. */
export type ProbePanel = ReturnType<typeof createProbePanel>;
