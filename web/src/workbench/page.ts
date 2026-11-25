/**
 * The workbench put together: the state (`state.ts`), the run and its column
 * store read along the line or at the point, the engine that draws the planes
 * (`engine.ts`) and the charts and the map around them.
 *
 * Every chart is a projection of three coordinates. A gesture moves one of
 * them, what it moved is drawn from what is in memory, and it is an entry of
 * the browser's history once it rests. A place read under the mouse moves none:
 * the charts beside it show it and go back when the mouse does.
 */

import { publishedOf, readPointSeries, seriesValues } from "../compare/series";
import { drawUnits } from "../crosssection/displayunits";
import { GROUND_ARRAYS, fillAvailable, sectionArrays } from "../crosssection/field";
import { clampLine, planSection, validateLine } from "../crosssection/line";
import { sectionPosition } from "../crosssection/panel";
import { linePreview } from "../crosssection/preview";
import { SectionError, createSectionSource, type SectionRead } from "../crosssection/source";
import type { LonLat, SectionDrawn, SectionFill, SectionLine, SectionPlan, SectionStore, SectionUnavailable } from "../crosssection/types";
import { element } from "../dom";
import { guardContextLoss } from "../glguard";
import { intlLocale, onLocaleChange, shellPath, t } from "../i18n";
import { FORECAST_MODELS } from "../manifest";
import { onMapPrefsChange } from "../mapprefs";
import { formatDistance } from "../measure";
import { supportsRangeRequests } from "../rangeprobe";
import { dataBaseUrl } from "../site";
import { altitude, windText } from "../sounding/summary";
import { isDark, onThemeChange } from "../theme";
import { displayZone, formatCompactStamp, formatDayMark, formatClockStamp, onDisplayZoneChange, setPinnedZone, zoneAt, zoneOffsetLabel } from "../timezone";
import { formatPrecipitation, formatPressure, formatTemperature, formatWind, onUnitsChange } from "../units";
import { sectionLine } from "../urlstate";
import { openZarrHubChannel } from "../zarr/channel";
import { HOUR, dayLabels, distanceLabels, lineAxis, timeAxis, timeRules } from "./axes";
import { Bar, type BarSwitch, type Coordinate, type Shown } from "./bar";
import type { LaneValues, PlaceReading, Plane } from "./engine";
import { SETTLE_MS, createHistory } from "./history";
import { LaneView, type LaneExtra } from "./lanes";
import { LANES, ROWS, workbenchLayout, type Layout } from "./layout";
import { KEY_DEGREES, Locator } from "./locator";
import { Pad, type PadCoordinate, type PadHandlers } from "./pad";
import { planPoint } from "./plan";
import { ProfileView, profileReading } from "./profile";
import { WorkbenchRenderer } from "./renderer";
import { createRun } from "./run";
import { FILLS, defaultState, nearestFrame, nearestLevel, parseState, placed, probeOnLine, searchForState, stepLevel, type WorkbenchState } from "./state";

/** Compressed chunks kept, for lines that share blocks with the last ones. */
const HELD_BYTES = 64 << 20;
/** Metres a press of the bar's arrows moves the probe along a line. */
const WHERE_STEP = 50_000;
/** How long the probe is still before its gusts and rain are read. */
const EXTRA_MS = 400;
/** Degrees either side of the probe a line is first drawn. */
const NEW_LINE = 8;

type Status =
  | { kind: "opening" | "reading" | "ready" | "failed" }
  | { kind: "none"; reason: SectionUnavailable };

export function mountWorkbench(root: HTMLElement): void {
  const byId = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
  const modelNode = byId<HTMLElement>("wb-model");
  const runNode = byId<HTMLElement>("wb-run");
  const noticeNode = byId<HTMLElement>("wb-notice");
  const action = byId<HTMLButtonElement>("wb-action");
  const copy = byId<HTMLButtonElement>("wb-copy");
  const openMap = byId<HTMLAnchorElement>("wb-open-map");

  let state: WorkbenchState = parseState(window.location.search);
  if (state.line && validateLine(state.line)) state.line = null;
  if (!state.line) state.point ??= defaultState().point;
  /** The state before the gesture under way, to go back to. */
  let before: WorkbenchState | null = null;
  /** The line last drawn, for a switch to the point and back. */
  let lastLine: SectionLine | null = state.line;
  const switches: Record<BarSwitch, boolean> = { wind: true, zero: true, theta: false, barbs: true };

  let status: Status = { kind: "opening" };
  let store: SectionStore | null = null;
  let plan: SectionPlan | null = null;
  let read: SectionRead | null = null;
  let renderer: WorkbenchRenderer | null = null;
  /** The engine has died once and been replaced. */
  let revived = false;
  let generation = 0;
  let complete = 0;
  let clamped: SectionLine | null = null;
  /** The place under the mouse, on the plane it is read on. */
  let hover: { plane: Plane; index: number } | null = null;
  let shown: Shown = "along";
  let layout: Layout = workbenchLayout(window.innerWidth, window.innerHeight, state.line !== null);
  let copied = false;
  /** The run on the page was taken because the one before had gone. */
  let taken = false;

  /** The leads of the store's frames, seconds, and its levels, hPa. */
  let leads: number[] = [];
  let levels: number[] = [];
  let point = 0;
  let frame = 0;
  let outside = false;

  const history = createHistory({
    search: () => searchForState(state, window.location.search),
    restored: (search) => {
      before = null;
      hover = null;
      const next = parseState(search);
      if (next.line && validateLine(next.line)) next.line = null;
      if (!next.line) next.point ??= defaultState().point;
      const moved = !samePlace(next, state);
      state = next;
      if (moved) begin(true);
      else place();
    },
  });

  const run = createRun(
    { baseUrl: dataBaseUrl(), model: state.model },
    {
      opened: () => begin(false),
      offered: render,
      failed: (error) => {
        console.warn("workbench: no run:", error.message);
        nothing("failed");
      },
    },
  );

  const source = createSectionSource({
    get manifest() {
      return run.current?.manifest ?? null;
    },
    get manifestUrl() {
      return run.current?.manifestUrl ?? null;
    },
    dataBackend: "zarr",
    streams: supportsRangeRequests,
    openChannel: () => openZarrHubChannel("columns"),
    heldBytes: HELD_BYTES,
  });

  // -------------------------------------------------------------------------
  // The page's parts.

  /** Whether two states are of the same line or cell, and so of the same read. */
  function samePlace(a: WorkbenchState, b: WorkbenchState): boolean {
    if (a.line && b.line) return String([a.line.a, a.line.b]) === String([b.line.a, b.line.b]);
    return !a.line && !b.line && String(a.point) === String(b.point);
  }

  function padHandlers(coordinate: "where" | "when" | null, plane: Plane | null): PadHandlers {
    const which = (of: PadCoordinate): Coordinate | null => (of === "level" ? "level" : coordinate);
    return {
      hover: (index) => {
        if (plane === null) return;
        if (index === null && hover?.plane !== plane) return;
        if (hover?.plane === plane && hover.index === index) return;
        hover = index === null ? null : { plane, index };
        place();
      },
      move: (of, value) => {
        const coordinateMoved = which(of);
        if (!coordinateMoved) return;
        before ??= state;
        if (coordinateMoved === "where") setPoint(value);
        else if (coordinateMoved === "when") setFrame(value);
        else setLevel(nearestLevel(levels.length > 0 ? levels : [value], value));
        place();
      },
      settle: () => {
        before = null;
        history.commit();
        render();
      },
      cancel: () => {
        if (before) state = before;
        before = null;
        place();
      },
      step: (of, by) => {
        const coordinateMoved = which(of);
        if (!coordinateMoved) return;
        if (coordinateMoved === "where") setPoint(point + by);
        else if (coordinateMoved === "when") setFrame(frame + by);
        else {
          const level = stepLevel(levels, state.level, by > 0 ? 1 : -1, state.top);
          if (level !== null) setLevel(level);
        }
        place();
        history.soon(render);
      },
      resized: () => resize(),
    };
  }

  const locator = new Locator({
    pressed: (at) => {
      if (!state.line) movePoint(at);
    },
    probed: (at) => {
      before ??= state;
      if (state.line && plan) setPoint(probeOnLine(plan.points, at));
      else state = { ...state, point: placed(at) };
      place();
    },
    settled: () => {
      const moved = before !== null && !samePlace(before, state);
      before = null;
      history.commit();
      if (moved) begin(false);
      else render();
    },
    cancelled: () => {
      if (before) state = before;
      before = null;
      place();
    },
    stepped: (east, north) => {
      if (state.line) {
        setPoint(point + (east !== 0 ? east : north));
        place();
        history.soon(render);
        return;
      }
      const [lon, lat] = state.point!;
      state = { ...state, point: placed([lon + east * KEY_DEGREES, Math.max(-90, Math.min(90, lat + north * KEY_DEGREES))]) };
      locator.show(null, state.point, false);
      history.soon(() => begin(false));
    },
    preview: (line) => linePreview(line.a, line.b, store, false),
    drew: (line, keyed) => {
      const next = sectionLine(line);
      if (validateLine(next)) {
        // Keys may have to go through a line that is none to one that is.
        if (!keyed) locator.refused();
        else drawnByKeys = null;
        return;
      }
      if (!keyed) return setLine(next);
      drawnByKeys = next;
      if (keyTimer !== null) clearTimeout(keyTimer);
      keyTimer = setTimeout(() => {
        keyTimer = null;
        if (drawnByKeys) setLine(drawnByKeys);
      }, SETTLE_MS);
    },
  });
  let drawnByKeys: SectionLine | null = null;
  let keyTimer: ReturnType<typeof setTimeout> | null = null;

  /** Another line, at rest: the charts of the one before are taken off, so that
   * none is ever of one line over another's axis. */
  function setLine(line: SectionLine): void {
    drawnByKeys = null;
    state = { ...state, line };
    history.commit();
    begin(false);
  }

  const mapRow = element("div", "wb-locator");
  const shapeGroup = element("div", "wb-group wb-shape");
  shapeGroup.setAttribute("role", "group");
  const shapeButtons = ([true, false] as const).map((hasLine) => {
    const node = element("button", "section-chip");
    node.type = "button";
    node.addEventListener("click", () => setShape(hasLine));
    shapeGroup.append(node);
    return { hasLine, node };
  });
  mapRow.append(locator.root, shapeGroup);

  const bar = new Bar({
    step: (coordinate, direction) => {
      if (coordinate === "level") {
        const level = stepLevel(levels, state.level, direction, state.top);
        if (level !== null) setLevel(level);
      } else if (coordinate === "when") setFrame(frame + direction);
      else if (state.line && plan) {
        const spacing = plan.length / Math.max(1, plan.points.length - 1);
        setPoint(point + direction * Math.max(1, Math.round(WHERE_STEP / spacing)));
      } else if (store && state.point) {
        const step = store.grid.nominalStepDegrees("longitude");
        movePoint([plan!.points[0]!.lon + direction * step, plan!.points[0]!.lat]);
        return;
      }
      place();
      history.commit();
    },
    fill: (fill) => restyle({ fill }),
    top: (top) => restyle({ top }),
    span: (span) => restyle({ span }),
    toggle: (name) => {
      switches[name] = !switches[name];
      place();
    },
    show: (next) => {
      shown = next;
      hover = null;
      relayout();
    },
  });

  function column(name: Shown): { root: HTMLElement; title: HTMLElement; held: HTMLElement; note: HTMLElement; chart: HTMLElement } {
    const section = element("section", `wb-column wb-column-${name}`);
    const head = element("div", "wb-head");
    const title = element("b", "wb-head-title");
    const held = element("span", "wb-head-held");
    const note = element("span", "wb-head-note");
    head.append(title, held, note);
    const chart = element("div", "wb-chart");
    section.append(head, chart);
    return { root: section, title, held, note, chart };
  }

  const along = column("along");
  const middle = column("profile");
  const through = column("through");
  const planes: Record<Plane, { pad: Pad; lanes: LaneView; axis: HTMLElement }> = {
    along: {
      pad: new Pad("wb-plane wb-where", padHandlers("where", "along")),
      lanes: new LaneView("wb-lane-pad wb-where", padHandlers("where", "along"), LANES - 1),
      axis: element("div", "wb-axis"),
    },
    through: {
      pad: new Pad("wb-plane wb-when", padHandlers("when", "through")),
      lanes: new LaneView("wb-lane-pad wb-when", padHandlers("when", "through")),
      axis: element("div", "wb-axis"),
    },
  };
  const profile = new ProfileView(padHandlers(null, null));
  const readout = element("output", "wb-readout");
  readout.setAttribute("aria-live", "polite");
  const numbers = element("dl", "wb-numbers");
  along.chart.append(planes.along.pad.root);
  along.root.append(element("div", "wb-readout"), planes.along.lanes.root, planes.along.axis);
  middle.chart.append(profile.root);
  middle.root.append(readout, numbers, profile.notes);
  through.chart.append(planes.through.pad.root);
  through.root.append(element("div", "wb-readout"), planes.through.lanes.root, planes.through.axis);
  const rules = element("div", "wb-rules");
  planes.through.pad.root.append(rules);
  const grid = element("div", "wb-grid");
  grid.append(along.root, middle.root, through.root);
  root.append(mapRow, bar.root, grid);

  // -------------------------------------------------------------------------
  // The coordinates.

  function setPoint(next: number): void {
    if (!plan || !state.line) return;
    point = Math.max(0, Math.min(plan.points.length - 1, next));
    const { lon, lat } = plan.points[point]!;
    state = { ...state, point: placed([lon, lat]) };
  }

  function setFrame(next: number): void {
    if (!store || leads.length === 0) return;
    const index = Math.max(0, Math.min(leads.length - 1, next));
    state = { ...state, time: store.runTime + leads[index]! * 1000 };
  }

  function setLevel(level: number): void {
    state = { ...state, level };
  }

  /** Another point, at rest: it is another read. */
  function movePoint(at: LonLat): void {
    const next = placed(at);
    if (String(next) === String(state.point)) return;
    state = { ...state, point: next };
    history.commit();
    begin(false);
  }

  function setShape(hasLine: boolean): void {
    if (hasLine === (state.line !== null)) return;
    const at = state.point ?? defaultState().point!;
    if (hasLine) {
      const line = lastLine ?? sectionLine({ a: [at[0] - NEW_LINE, at[1]], b: [at[0] + NEW_LINE, at[1]] });
      state = { ...state, line };
    } else {
      lastLine = state.line;
      state = { ...state, line: null, point: at, fill: state.fill === "cross" ? "wind" : state.fill };
    }
    hover = null;
    history.commit();
    begin(true);
  }

  function restyle(change: Partial<Pick<WorkbenchState, "fill" | "top" | "span">>): void {
    state = { ...state, ...change };
    history.replace();
    place();
  }

  // -------------------------------------------------------------------------
  // The read.

  function stop(): void {
    generation += 1;
    read?.cancel();
    read = null;
    plan = null;
    complete = 0;
    hover = null;
    renderer?.clear();
    showLanes("along", null);
    showLanes("through", null);
    shownPlace = { reading: null, column: null };
    lanesKey = { along: "", through: "" };
  }

  /** A read of the run failed. Once the next run is out this one is deleted,
   * and the page then takes the next whole rather than show two. */
  async function replaced(): Promise<boolean> {
    if (!run.current) return false;
    await run.check();
    if (!run.newer) return false;
    taken = true;
    run.take();
    return true;
  }

  function nothing(reason: SectionUnavailable): void {
    stop();
    status = { kind: "none", reason };
    render();
  }

  function begin(aim: boolean): void {
    start(aim).catch((error: unknown) => console.warn("workbench:", error));
  }

  async function start(aim: boolean): Promise<void> {
    stop();
    if (!source.current) {
      source.end();
      store = null;
    }
    const sequence = generation;
    if (!run.current) return;
    status = { kind: "opening" };
    locator.show(state.line, probePlace(), aim);
    relayout();
    let opened: SectionStore;
    let planned: SectionPlan;
    try {
      opened = await source.open();
      if (sequence !== generation) return;
      const held = sectionArrays(opened);
      if (!GROUND_ARRAYS.every((id) => held.has(id))) throw new SectionError("model", "the store lacks the arrays of a section");
      if (state.line) {
        const cut = clampLine(state.line, opened);
        if (cut.clamped) {
          state = { ...state, line: sectionLine(cut.line) };
          clamped = state.line;
          history.replace();
        }
        planned = planSection(state.line!, opened);
      } else planned = planPoint(state.point!, opened);
      if (planned.cells.length === 0) throw new SectionError("outside", "nothing of it is on the grid");
      store = opened;
      plan = planned;
      const reference = held.get("tmp") ?? held.get("hgt")!;
      leads = reference.offsets;
      levels = held.get("hgt")!.levels ?? [];
      ensureRenderer().setSection(opened, planned);
      status = { kind: "reading" };
    } catch (error) {
      if (sequence !== generation) return;
      console.warn("workbench: not opened:", error instanceof Error ? error.message : error);
      const reason = error instanceof SectionError ? error.reason : "failed";
      if (reason === "failed" && (await replaced())) return;
      if (sequence === generation) nothing(reason);
      return;
    }
    if (state.line) lastLine = state.line;
    locator.show(state.line, probePlace(), aim);
    void followZone(sequence);
    relayout();
    const painter = renderer!;
    const ids = [...sectionArrays(opened).values()].map(({ id }) => id);
    const reading = source.read(planned, ids, (array, indices, codes) => {
      if (sequence === generation) renderer?.addCells(array, indices, codes);
    });
    read = reading;
    try {
      await reading.done;
      if (sequence === generation) status = { kind: "ready" };
    } catch (error) {
      if (sequence !== generation) return;
      console.warn("workbench: read stopped:", error instanceof Error ? error.message : error);
      if (await replaced()) return;
      if (sequence !== generation) return;
      status = { kind: "failed" };
    }
    if (sequence === generation && painter === renderer) {
      lanesKey = { along: "", through: "" };
      place();
    }
  }

  /** A point's clock is its own place's, as a pin's is. */
  async function followZone(sequence: number): Promise<void> {
    if (state.line) return setPinnedZone(null);
    const zone = await zoneAt(state.point![0], state.point![1]);
    if (sequence === generation) setPinnedZone(zone);
  }

  function ensureRenderer(): WorkbenchRenderer {
    if (renderer) return renderer;
    const canvases = {} as Record<Plane, HTMLCanvasElement>;
    for (const plane of ["along", "through"] as const) {
      const { pad } = planes[plane];
      pad.root.querySelector(".wb-plane-canvas")?.remove();
      const canvas = element("canvas", "wb-plane-canvas");
      canvas.setAttribute("aria-hidden", "true");
      pad.root.prepend(canvas);
      canvases[plane] = canvas;
    }
    const made: WorkbenchRenderer = new WorkbenchRenderer(canvases, {
      onDrawn,
      onFailed: () => {
        if (renderer !== made) return;
        renderer = null;
        if (revived) return nothing("failed");
        revived = true;
        begin(false);
      },
    });
    renderer = made;
    resize();
    return made;
  }

  function onDrawn(_plane: Plane, drawn: SectionDrawn): void {
    if (drawn.complete !== complete) {
      complete = drawn.complete;
      lanesKey = { along: "", through: "" };
      render();
      readPlace();
      readLanes();
    }
  }

  function resize(): void {
    const dpr = window.devicePixelRatio || 1;
    for (const plane of ["along", "through"] as const) {
      const { width, height } = planes[plane].pad;
      if (width > 0 && height > 0) renderer?.size(plane, width, height, dpr);
    }
    render();
  }

  // -------------------------------------------------------------------------
  // What is drawn and read of the coordinates.

  /** The probe, as the map marks it. */
  function probePlace(): LonLat | null {
    const at = state.line ? plan?.points[point] : null;
    return at ? [at.lon, at.lat] : state.point;
  }

  /** The place and time the profile is of. */
  function readAt(): { point: number; lead: number } {
    const lead = leads[frame] ?? 0;
    if (hover?.plane === "along") return { point: hover.index, lead };
    if (hover?.plane === "through") return { point, lead: leads[hover.index] ?? lead };
    return { point, lead };
  }

  /** Bring everything in line with the state, without reading any data. */
  function place(): void {
    if (store && plan) {
      point = state.line ? probeOnLine(plan.points, state.point) : 0;
      const found = nearestFrame(leads, store.runTime, state.time ?? Date.now());
      frame = found.frame;
      outside = found.outside && state.time !== null;
      draw();
    }
    readPlace();
    readLanes();
    readExtra();
    render();
  }

  function draw(): void {
    if (!renderer || !store || !plan) return;
    renderer.draw({
      section: {
        lead: leads[frame] ?? 0,
        fill: state.fill,
        lines: { wind: switches.wind, zero: switches.zero, theta: switches.theta },
        barbs: switches.barbs,
        top: state.top,
        mapLevels: { fill: null, lines: null },
        dark: isDark,
        units: drawUnits(),
      },
      point,
      columns: { first: leads[0] ?? 0, step: HOUR, count: state.span + 1 },
    });
  }

  let shownPlace: PlaceReading = { reading: null, column: null };
  let readingPlace = false;
  let placeDue = false;
  /** Read the profile and the line under it: one at a time, of where they are
   * by then. */
  function readPlace(): void {
    if (!renderer || !plan) return;
    if (readingPlace) {
      placeDue = true;
      return;
    }
    readingPlace = true;
    const sequence = generation;
    const at = readAt();
    void renderer.place(at.point, at.lead, state.level).then((found) => {
      readingPlace = false;
      if (sequence === generation) {
        shownPlace = found;
        renderProfile();
      }
      if (placeDue) {
        placeDue = false;
        readPlace();
      }
    });
  }

  let lanesKey: Record<Plane, string> = { along: "", through: "" };
  const readingLanes: Record<Plane, boolean> = { along: false, through: false };
  function readLanes(): void {
    if (!renderer || !plan) return;
    for (const plane of ["along", "through"] as const) {
      const key = plane === "along" ? `${generation}/${leads[frame]}` : `${generation}/${point}`;
      if (readingLanes[plane] || lanesKey[plane] === key) continue;
      lanesKey[plane] = key;
      readingLanes[plane] = true;
      const sequence = generation;
      const asked = plane === "along" ? renderer.along(leads[frame] ?? 0) : renderer.through(point);
      void asked.then((lanes) => {
        readingLanes[plane] = false;
        if (sequence === generation) showLanes(plane, lanes);
        readLanes();
      });
    }
  }

  const shownLanes: Record<Plane, LaneValues | null> = { along: null, through: null };
  function showLanes(plane: Plane, lanes: LaneValues | null): void {
    shownLanes[plane] = lanes;
    planes[plane].lanes.show(lanes, plane === "through" ? extra : undefined);
  }

  /** The gusts and the rain at the probe, a value a frame: the column store has
   * neither, and they come from the run's own stores once the probe rests. */
  let extra: LaneExtra = { gust: null, rain: null };
  let extraKey = "";
  let extraTimer: ReturnType<typeof setTimeout> | null = null;
  let extraRead: AbortController | null = null;

  function readExtra(): void {
    const loaded = run.current;
    const at = plan?.points[point];
    const key = loaded && at ? `${loaded.manifestUrl} ${at.lon},${at.lat}` : "";
    if (key === extraKey) return;
    extraKey = key;
    extraRead?.abort();
    extraRead = null;
    if (extraTimer !== null) clearTimeout(extraTimer);
    extraTimer = null;
    if (extra.gust || extra.rain) {
      extra = { gust: null, rain: null };
      showLanes("through", shownLanes.through);
    }
    if (!loaded || !at) return;
    extraTimer = setTimeout(() => {
      extraTimer = null;
      const controller = new AbortController();
      extraRead = controller;
      for (const [name, bundle] of [["gust", "gust"], ["rain", "prate"]] as const) {
        if (publishedOf(loaded, [bundle]).length === 0) continue;
        readPointSeries(loaded, bundle, at.lon, at.lat, controller.signal)
          .then((series) => {
            if (controller.signal.aborted || !series) return;
            const values = seriesValues(series);
            const byTime = new Map(series.validTimesMs.map((time, index) => [time, values[index] ?? null]));
            extra = { ...extra, [name]: Float32Array.from(leads, (_, index) => byTime.get(timeOf(index)) ?? Number.NaN) };
            showLanes("through", shownLanes.through);
            renderProfile();
          })
          .catch((error: unknown) => {
            if (controller.signal.aborted) return;
            console.warn(`workbench: ${bundle} not read:`, error instanceof Error ? error.message : error);
            void replaced();
          });
      }
    }, EXTRA_MS);
  }

  // -------------------------------------------------------------------------
  // Words and marks.

  function stamp(time: number): string {
    return formatCompactStamp(time, displayZone);
  }

  function timeOf(index: number): number {
    return (store?.runTime ?? 0) + (leads[index] ?? 0) * 1000;
  }

  function position(index: number): string {
    const at = plan?.points[index];
    return at ? sectionPosition([at.lon, at.lat], state.line ? 1 : 2) : "";
  }

  function alongText(index: number): string {
    return plan ? t("wbFromA", { distance: formatDistance(plan.points[index]?.along ?? 0) }) : "";
  }

  function unavailable(reason: SectionUnavailable): string {
    const model = FORECAST_MODELS[state.model].label;
    switch (reason) {
      case "model":
        return t("sectionNoModel", { model });
      case "run":
        return t("sectionNoRun");
      case "origin":
        return t("sectionNoRanges");
      case "outside":
        return t("probeOutside");
      case "failed":
        return t("sectionFailed");
    }
  }

  function runName(runTime: string): string {
    return `${runTime.slice(0, 10)} ${runTime.slice(11, 13)}Z`;
  }

  function renderHeader(): void {
    const { current, newer } = run;
    modelNode.textContent = FORECAST_MODELS[state.model].label;
    runNode.textContent = current ? runName(current.manifest.runTime) : "";
    const said: string[] = [];
    if (status.kind === "none") said.push(unavailable(status.reason));
    else if (status.kind === "opening") said.push(t("wbOpening", { model: FORECAST_MODELS[state.model].label }));
    else if (status.kind === "failed") said.push(t("sectionFailed"));
    else if (status.kind === "reading") said.push(t("sectionLoading", { percent: Math.min(99, Math.floor(complete * 100)) }));
    if (state.line && state.line === clamped && plan) said.push(t("sectionClamped", { length: formatDistance(plan.length) }));
    if (outside && plan) said.push(t("wbTimeOutside"));
    if (taken && current) said.push(t("wbRunReplaced", { run: runName(current.manifest.runTime) }));
    if (newer) said.push(t("wbNewRun", { run: runName(newer.manifest.runTime) }));
    noticeNode.textContent = said.join(" · ");
    const failed = status.kind === "failed" || (status.kind === "none" && status.reason === "failed");
    const other = status.kind === "none" && status.reason === "model" && state.model !== "gfs";
    action.hidden = !(newer || failed || other);
    action.textContent = other
      ? t("wbUseModel", { model: FORECAST_MODELS.gfs.label })
      : newer
        ? t("wbTakeRun", { run: runName(newer.manifest.runTime) })
        : t("retry");
    copy.textContent = t(copied ? "wbLinkCopied" : "wbCopyLink");
    const params = new URLSearchParams();
    if (state.line) params.set("section", [state.line.a[1], state.line.a[0], state.line.b[1], state.line.b[0]].map((value) => value.toFixed(2)).join(","));
    else if (state.point) params.set("point", `${state.point[1].toFixed(2)},${state.point[0].toFixed(2)}`);
    openMap.href = `${shellPath(`/${state.model}`)}?${params.toString().replaceAll("%2C", ",")}`;
  }

  function renderBar(): void {
    const time = timeOf(frame);
    const lead = Math.round((leads[frame] ?? 0) / HOUR);
    const held = store && sectionArrays(store);
    const height = shownPlace.reading?.height;
    bar.render({
      where: plan
        ? state.line
          ? [position(point), alongText(point)]
          : [sectionPosition(state.point!, 2), t("wbCell", { place: position(0) })]
        : [state.point ? sectionPosition(state.point, 2) : "", ""],
      when: plan ? [`${formatDayMark(time, displayZone, intlLocale)} ${formatClockStamp(time, displayZone)}`, `F${String(lead).padStart(3, "0")} · ${zoneOffsetLabel(displayZone, time)}`] : ["", ""],
      level: [`${state.level} hPa`, height !== undefined && Number.isFinite(height) ? altitude(height) : ""],
      fill: state.fill,
      hasLine: state.line !== null,
      fills: store && held ? FILLS.filter((fill: SectionFill) => fillAvailable(fill, store!)) : FILLS,
      top: state.top,
      span: state.span,
      switches,
      choices:
        layout.arrangement === "phone"
          ? state.line
            ? ["along", "profile", "through"]
            : ["profile", "through"]
          : layout.arrangement === "one"
            ? ["along", "through"]
            : [],
      shown: shownNow(),
    });
  }

  /** The chart shown where not all of them are. */
  function shownNow(): Shown {
    if (layout.arrangement === "phone") return shown === "along" && !state.line ? "profile" : shown;
    return shown === "profile" ? "along" : shown;
  }

  function renderHeads(): void {
    const at = readAt();
    const time = timeOf(frame);
    along.title.textContent = t("wbAlong");
    along.held.textContent = plan ? stamp(time) : "";
    along.note.textContent = plan && state.line ? `A–B ${formatDistance(plan.length)}` : "";
    through.title.textContent = t("wbThrough");
    through.held.textContent = plan ? position(point) : "";
    through.note.textContent = "";
    middle.title.textContent = t("wbHere");
    middle.held.textContent = hover === null ? "" : hover.plane === "along" ? position(at.point) : stamp((store?.runTime ?? 0) + at.lead * 1000);
    middle.held.dataset.coordinate = hover?.plane === "along" ? "where" : "when";
    middle.note.textContent = plan ? `${FORECAST_MODELS[state.model].label} · ${state.line ? t("wbBlended") : t("wbCell", { place: position(0) })}` : "";
    profile.root.dataset.hover = hover === null ? "" : hover.plane === "along" ? "where" : "when";
  }

  function renderProfile(): void {
    const { column: read, reading } = shownPlace;
    profile.show(read && profileReading(read), state.top, layout.plot);
    const known = (value: number | undefined, text: (value: number) => string): string => (value !== undefined && Number.isFinite(value) ? text(value) : "--");
    if (!reading) readout.textContent = "";
    else if (reading.belowGround) readout.textContent = `${Math.round(reading.pressure)} hPa · ${t("sectionBelowGround", { pressure: `${Math.round(reading.surfacePressure)} hPa` })}`;
    else {
      const wind = Number.isFinite(reading.windDirection) && Number.isFinite(reading.windSpeed) ? windText(reading.windDirection, reading.windSpeed) : "--";
      readout.textContent = [
        `${Math.round(reading.pressure)} hPa`,
        `T ${known(reading.temperature, (value) => formatTemperature(value))}`,
        known(reading.humidity, (value) => `${Math.round(value)} %`),
        wind,
        `θe ${known(reading.thetaE, (value) => `${value.toFixed(1)} K`)}`,
      ].join(" · ");
    }
    const surface = read?.surface;
    const value = (number: number | null | undefined, text: (value: number) => string): string => (number === null || number === undefined ? "--" : text(number));
    const speed = surface && surface.u !== null && surface.v !== null ? Math.hypot(surface.u, surface.v) : null;
    // They are read at the probe, and not at a place along the line under the
    // mouse.
    const index = hover?.plane === "along" ? -1 : hover?.plane === "through" ? hover.index : frame;
    const extraAt = (values: ArrayLike<number> | null, text: (value: number) => string): string => known(values?.[index], text);
    const rows: [string, string][] = [
      [t("wbLaneTemperature"), `${value(surface?.t, (number) => formatTemperature(number))} · ${value(surface?.td, (number) => formatTemperature(number))}`],
      [t("wbLaneWind"), `${value(speed, (number) => formatWind(number))} · ${extraAt(extra.gust, (number) => formatWind(number))}`],
      [t("wbLanePressure"), value(surface?.prmsl, (number) => formatPressure(number, 0))],
      [t("wbLaneRain"), extraAt(extra.rain, (number) => `${formatPrecipitation(number)}/h`)],
    ];
    numbers.replaceChildren(
      ...rows.map(([name, text]) => {
        const row = element("div", "wb-number");
        row.append(element("dt", "", name), element("dd", "", text));
        return row;
      }),
    );
    renderBar();
  }

  function renderMarks(): void {
    const onAlong = state.line !== null && plan !== null;
    const alongHover = hover?.plane === "along" ? hover.index : null;
    const throughHover = hover?.plane === "through" ? hover.index : null;
    const alongAxis = onAlong ? lineAxis(plan!.points.length) : null;
    const throughAxis = plan && leads.length > 0 ? timeAxis(leads, state.span) : null;
    const axes: Record<Plane, typeof alongAxis> = { along: alongAxis, through: throughAxis };
    const marks = {
      along: { index: point, level: state.level, hover: alongHover, hoverLabel: alongHover === null || !plan ? "" : formatDistance(plan.points[alongHover]?.along ?? 0) },
      through: { index: frame, level: state.level, hover: throughHover, hoverLabel: throughHover === null ? "" : stamp(timeOf(throughHover)) },
    };
    for (const plane of ["along", "through"] as const) {
      const { pad, lanes } = planes[plane];
      pad.setAxis(axes[plane]);
      pad.setTop(plan ? state.top : null);
      pad.setMarks(marks[plane]);
      lanes.setAxis(axes[plane]);
      lanes.pad.setMarks({ ...marks[plane], level: null, hoverLabel: "" });
      pad.root.setAttribute("aria-label", t(plane === "along" ? "sectionPlotAria" : "wbThroughAria"));
      lanes.pad.root.setAttribute("aria-label", t("wbLanesAria"));
    }
    profile.pad.setTop(plan ? state.top : null);
    profile.pad.setMarks({ index: null, level: state.level, hover: null, hoverLabel: "" });
    profile.pad.root.setAttribute("aria-label", t("wbProfileAria"));
    const width = planes.through.pad.width;
    planes.along.axis.replaceChildren(...(onAlong ? distanceLabels(plan!.length, planes.along.pad.width) : []));
    const from = timeOf(0);
    planes.through.axis.replaceChildren(...(plan ? dayLabels(from, state.span, width) : []));
    rules.replaceChildren(...(plan ? timeRules(from, state.span, Date.now()) : []));
  }

  let renderDue = false;
  function render(): void {
    if (renderDue) return;
    renderDue = true;
    requestAnimationFrame(() => {
      renderDue = false;
      renderHeader();
      renderBar();
      renderHeads();
      renderMarks();
      locator.show(state.line, probePlace(), false);
      markHover();
      for (const { hasLine, node } of shapeButtons) {
        node.textContent = t(hasLine ? "wbLine" : "wbPoint");
        node.setAttribute("aria-pressed", String(hasLine === (state.line !== null)));
      }
      shapeGroup.setAttribute("aria-label", t("wbShapeAria"));
    });
  }

  let marked: number | null = null;
  function markHover(): void {
    const index = hover?.plane === "along" ? hover.index : null;
    if (index === marked) return;
    marked = index;
    const at = index === null ? null : plan?.points[index];
    locator.setHover(at ? [at.lon, at.lat] : null);
  }

  function relayout(): void {
    layout = workbenchLayout(window.innerWidth, window.innerHeight, state.line !== null, bar.root.offsetHeight || ROWS.bar);
    root.style.setProperty("--wb-locator", `${layout.locator}px`);
    root.style.setProperty("--wb-plot", `${layout.plot}px`);
    root.style.setProperty("--wb-gutter", `${ROWS.gutter}px`);
    root.style.setProperty("--wb-lanes", `${layout.lanes ? LANES * ROWS.lane : 0}px`);
    root.dataset.arrangement = layout.arrangement;
    root.dataset.lanes = String(layout.lanes);
    root.dataset.shape = state.line ? "line" : "point";
    root.dataset.shown = shownNow();
    locator.resize();
    locator.setEditable(layout.arrangement !== "phone");
    renderProfile();
    place();
  }

  function refresh(): void {
    locator.refresh();
    profile.refresh();
    for (const { lanes } of Object.values(planes)) lanes.paint();
    draw();
    renderProfile();
    render();
  }

  action.addEventListener("click", () => {
    if (status.kind === "none" && status.reason === "model" && state.model !== "gfs") {
      const params = new URLSearchParams(window.location.search);
      params.delete("model");
      window.location.search = params.toString();
    } else if (run.newer) {
      taken = false;
      run.take();
    }
    else if (run.current) begin(false);
    else void run.retry();
  });
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(window.location.href).then(
      () => {
        copied = true;
        render();
        setTimeout(() => {
          copied = false;
          render();
        }, 2000);
      },
      () => (noticeNode.textContent = t("copyFailed")),
    );
  });
  window.addEventListener("resize", relayout);
  for (const subscribe of [onLocaleChange, onUnitsChange, onMapPrefsChange, onThemeChange, onDisplayZoneChange]) subscribe(refresh);

  relayout();
  if (!locator.open()) locator.root.append(element("p", "wb-no-map", t("wbNoMap")));
  else guardContextLoss(locator.root, () => locator.root.replaceChildren(element("p", "wb-no-map", t("wbNoMap"))));
  locator.show(state.line, state.point, true);
  void run.start();
}
