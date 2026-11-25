/**
 * The vertical cross-section's controller: the line (`view.section`), drawn
 * by one drag and kept while datasets change under it, the read of the run's
 * column store along it, and the panel and the map layer that show it.
 *
 * The section follows the playhead and never holds it: a frame on the map
 * has the section drawn at its valid time, whenever the section's own data
 * have arrived. A run that ends takes the chart off at once and the next one
 * is read whole, so no chart is ever of two runs; one opened again beside an
 * unchanged store (a bundle added to it) has its chart back as it was.
 *
 * The panel has the probe panel's slot. While a point is pinned it steps
 * aside, and comes back when the pin goes.
 *
 * The page knows `shell.ts`, which loads this when there is a line.
 */

import type { Map as MaplibreMap } from "maplibre-gl";

import { escapes } from "../escapestack";
import { onLocaleChange, t } from "../i18n";
import { bundleLevel, familyOf } from "../levels";
import type { ForecastManifest } from "../manifest";
import { onMapPrefsChange } from "../mapprefs";
import { distance, formatDistance, greatCircle } from "../measure";
import { isGlobeProjection } from "../projection";
import { isDark, onThemeChange } from "../theme";
import { onUnitsChange } from "../units";
import { sectionLine } from "../urlstate";
import type { ViewState } from "../viewstate";
import type { LinePreview } from "../volumetool";
import { lineAim } from "./aim";
import { drawUnits } from "./displayunits";
import { fillAvailable, GROUND_ARRAYS, sectionArrays } from "./field";
import { clampLine, MIN_LENGTH, planSection, validateLine } from "./line";
import { SectionMapLayer } from "./maplayer";
import { SECTION_FILLS, SectionPanel, type SectionCursor, type SectionOptions } from "./panel";
import { SectionRenderer } from "./renderer";
import { SectionError, type SectionRead, type SectionSource } from "./source";
import type { LonLat, SectionDrawn, SectionFill, SectionLine, SectionPlan, SectionStore, SectionUnavailable } from "./types";

/** What the cross-section reads off the page, the mutable parts as getters. */
export interface CrossSectionDeps {
  readonly map: MaplibreMap;
  readonly view: ViewState;
  readonly source: SectionSource;
  /** What the panel is put in front of. */
  readonly capsule: HTMLElement;
  /** The run on screen; null while one is being opened. */
  readonly manifest: ForecastManifest | null;
  /** The dataset's name, as the page's title has it. */
  readonly modelLabel: string;
  /** The valid time of the frame on screen, or null. */
  readonly playhead: () => number | null;
  readonly closeProbe: () => void;
  readonly syncUrl: () => void;
  /** Whether a line is set has changed. */
  readonly onTool: () => void;
}

/** Where the section stands: `waiting` for a run, its store `opening`, its
 * cells `reading`, all of it `ready`, stopped by a failed read (`failed`, what
 * had arrived still drawn), or nothing to draw (`none`), in the words it was
 * said in: of the dataset then on screen. */
type Status =
  | { kind: "waiting" | "opening" | "reading" | "ready" | "failed" }
  | { kind: "none"; reason: SectionUnavailable; model: string; retry: boolean };

/** What a line drawn over a field is filled with: the field's own quantity
 * where the section has it, else the one that shows fronts. */
export function fillForField(field: ViewState["field"]): SectionFill {
  const family = field === null ? null : familyOf(field);
  if (family === "tmp" || family === "rh" || family === "wind") return family;
  return "thetae";
}

/** How far from a point of the line the place in sight where it projects may
 * be, before the point is behind the globe. */
const HIDDEN_METRES = 500_000;

/** The share of a section that has arrived, as its notice has it. */
function percent(complete: number): number {
  return Math.min(99, Math.floor(complete * 100));
}

export function createCrossSection(deps: CrossSectionDeps) {
  const { map, view, source } = deps;
  const layer = new SectionMapLayer(map);
  const options: SectionOptions = {
    fill: fillForField(view.field),
    lines: { wind: true, zero: true, theta: false },
    barbs: true,
    top: 100,
  };
  let status: Status = { kind: "waiting" };
  let store: SectionStore | null = null;
  let plan: SectionPlan | null = null;
  let read: SectionRead | null = null;
  let drawn: SectionDrawn | null = null;
  /** The line that is what was left of a longer one. */
  let cutLine: SectionLine | null = null;
  /** The run is over and the next not yet open: nothing of the chart is
   * shown, and nothing of it has been let go of. */
  let suspended = false;
  /** There has been a chart to show since there last was nothing to: the panel
   * keeps its size from one to the next. */
  let full = false;
  /** Moves with every start and every stop: what a section since replaced
   * still has to say is dropped. */
  let generation = 0;
  /** The line on screen is new, to the page or to the map, and the map has
   * not yet been moved out from under the panel for it. */
  let aimDue = view.section !== null;
  let probeShown = false;
  let cursor: SectionCursor | null = null;
  /** The panel is made for the first line, the renderer for the first store
   * there is to draw of. */
  let panel: SectionPanel | null = null;
  let renderer: SectionRenderer | null = null;
  let plot = { width: 0, height: 0 };
  const surface = { isOpen: () => panel !== null && !panel.root.hidden, close: () => clear() };

  if (view.section && validateLine(view.section)) {
    view.section = null;
    deps.syncUrl();
    deps.onTool();
  }

  function ensurePanel(): SectionPanel {
    if (panel) return panel;
    const made = new SectionPanel({
      onClose: () => {
        clear();
        // The button the focus was on has gone with the panel.
        map.getCanvas().focus({ preventScroll: true });
      },
      onRetry: begin,
      onFill: (fill) => {
        options.fill = fill;
        changed();
      },
      onSwitch: (name) => {
        if (name === "barbs") options.barbs = !options.barbs;
        else options.lines[name] = !options.lines[name];
        changed();
      },
      onTop: (top) => {
        options.top = top;
        changed();
      },
      onResize: (width, height) => {
        plot = { width, height };
        resize();
        draw();
      },
      onCursor: (at) => {
        cursor = at;
        const point = at && plan?.points[at.point];
        layer.setHover(point ? [point.lon, point.lat] : null);
        readCursor(true);
      },
    });
    panel = made;
    deps.capsule.before(made.root);
    // Once, for a line a link brought: a run arriving later under an open
    // sheet does not go before it.
    escapes.opened(surface);
    return made;
  }

  function resize(): void {
    if (plot.width > 0) renderer?.resize(plot.width, plot.height, window.devicePixelRatio || 1);
  }

  function ensureRenderer(): SectionRenderer {
    if (renderer) return renderer;
    const made = new SectionRenderer(ensurePanel().freshCanvas(), {
      onDrawn: (now) => {
        drawn = now;
        render();
        readCursor(false);
      },
      // Its canvas is gone with it: what asks again has another.
      onFailed: () => {
        made.destroy();
        renderer = null;
        if (!view.section) return;
        stop();
        nothing("failed", true);
      },
    });
    renderer = made;
    resize();
    return made;
  }

  /** The fills a store has the arrays of. */
  function fillsOf(of: SectionStore): SectionFill[] {
    const held = sectionArrays(of);
    if (!GROUND_ARRAYS.every((id) => held.has(id))) return [];
    return SECTION_FILLS.filter((fill) => fillAvailable(fill, of));
  }

  /** The reader's fill, or the first the store has in its place. */
  function fillShown(): SectionFill {
    const fills = store ? fillsOf(store) : [];
    return fills.length === 0 || fills.includes(options.fill) ? options.fill : fills[0]!;
  }

  function changed(): void {
    draw();
    render();
  }

  let readingCursor = false;
  let aloud = false;
  /** Read the chart at the crosshair: one reading at a time, of where the
   * crosshair is by then. It is spoken where the crosshair has moved, not
   * where a frame has passed under it. */
  function readCursor(moved: boolean): void {
    aloud ||= moved;
    if (!renderer || !cursor || readingCursor) return;
    const at = cursor;
    const speak = aloud;
    aloud = false;
    readingCursor = true;
    void renderer
      .probe(at.point, at.pressure)
      .catch(() => null)
      .then((reading) => {
        readingCursor = false;
        if (cursor === at) panel?.showReading(reading, speak);
        else aloud ||= speak;
        if (cursor !== at || aloud) readCursor(false);
      });
  }

  /** Draw the section at the playhead. */
  function draw(): void {
    const time = deps.playhead();
    if (!renderer || !store || !plan || time === null || suspended || !surface.isOpen()) return;
    renderer.draw({
      lead: (time - store.runTime) / 1000,
      fill: fillShown(),
      lines: { ...options.lines },
      barbs: options.barbs,
      top: options.top,
      mapLevels: {
        fill: view.field === null ? null : bundleLevel(view.field),
        lines: view.lines === null ? null : bundleLevel(view.lines),
      },
      dark: isDark,
      units: drawUnits(),
    });
  }

  function unavailableText(reason: SectionUnavailable, model: string): string {
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

  function notices(): string[] {
    const said: string[] = [];
    if (status.kind === "none") return [unavailableText(status.reason, status.model)];
    if (suspended || status.kind === "waiting") return [t("readingData")];
    if (status.kind === "failed") said.push(t("sectionFailed"));
    else if (status.kind !== "ready") said.push(t("sectionLoading", { percent: percent(status.kind === "reading" ? (drawn?.complete ?? 0) : 0) }));
    else if (drawn?.noFrame) said.push(t("sectionNoFrame"));
    const run = deps.manifest;
    // A borrowed section says whose it is, and on which grid.
    if (store && run && !run.columns && run.borrowed?.columns) said.push(`${store.model} ${stepDegrees(store)}°`);
    if (plan && view.section === cutLine) said.push(t("sectionClamped", { length: formatDistance(plan.length) }));
    return said;
  }

  function stepDegrees(from: SectionStore): number {
    return Number(from.grid.nominalStepDegrees().toFixed(2));
  }

  function render(): void {
    const line = view.section;
    if (!line) {
      if (panel) panel.root.hidden = true;
      return;
    }
    const made = ensurePanel();
    const shown = suspended ? null : plan;
    const wasOpen = surface.isOpen();
    made.root.hidden = probeShown;
    made.render({
      line,
      length: plan?.length ?? null,
      points: shown?.points ?? null,
      notices: notices(),
      retry: status.kind === "none" ? status.retry : status.kind === "failed" && !suspended,
      collapsed: !full,
      blank: shown === null,
      options: { ...options, fill: fillShown() },
      fills: store ? fillsOf(store) : SECTION_FILLS,
      heights: shown ? (drawn?.heights ?? []) : [],
      step: store ? stepDegrees(store) : null,
    });
    // The frames that went by while it was out of sight were not drawn.
    if (!wasOpen && surface.isOpen()) draw();
  }

  /** Move the map, once, if the panel has come to cover the line. */
  function aim(): void {
    const line = view.section;
    if (!line || !surface.isOpen()) return;
    // After the layout the panel's opening asked for has happened.
    requestAnimationFrame(() => {
      if (view.section !== line || !panel || !surface.isOpen()) return;
      const box = map.getContainer().getBoundingClientRect();
      const path = greatCircle(line.a, line.b);
      const middle = path[path.length >> 1]!;
      // The copy of the world the map is on.
      const turns = 360 * Math.round((map.getCenter().lng - middle[0]) / 360);
      const points = path.map(([lon, lat]) => map.project([lon + turns, lat]));
      const move = lineAim(
        points.map(({ x, y }) => ({ x: box.left + x, y: box.top + y })),
        box,
        panel.root.getBoundingClientRect(),
        // Under a tilt and on the globe the line changes shape as it moves.
        map.getPitch() > 0 || isGlobeProjection(map) ? 0.85 : 1,
      );
      if (!move) return;
      // A part of it behind the globe projects onto the side in sight, and
      // the middle of that is no place of the line's.
      const behind = path.some(([lon, lat], index) => {
        const { lng, lat: back } = map.unproject(points[index]!);
        return distance([lng, back], [lon, lat]) > HIDDEN_METRES;
      });
      const center = behind
        ? { lng: middle[0] + turns, lat: middle[1] }
        : map.unproject([move.from.x - box.left, move.from.y - box.top]);
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      map.easeTo({
        center,
        offset: move.offset,
        duration: reduced ? 0 : 450,
        // With none given the globe keeps its size as the latitude changes.
        ...(move.scale < 1 && !behind && { zoom: map.getZoom() + Math.log2(move.scale) + globeZoomShift(center.lat) }),
      });
    });
  }

  /** What a globe's zoom changes by for the same size of globe at another
   * latitude; nothing on the plane. */
  function globeZoomShift(latitude: number): number {
    if (!isGlobeProjection(map)) return 0;
    const cosine = (degrees: number): number => Math.max(0.05, Math.cos((degrees * Math.PI) / 180));
    return Math.log2(cosine(latitude) / cosine(map.getCenter().lat));
  }

  /** Put the line on the map as it now stands. */
  function showLine(): void {
    layer.setLine(view.section, status.kind !== "none");
  }

  /** There is nothing to draw, for a reason. */
  function nothing(reason: SectionUnavailable, retry: boolean): void {
    status = { kind: "none", reason, model: deps.modelLabel, retry };
    full = false;
    aimDue = false;
    showLine();
    render();
    panel?.announce(notices().join(" · "));
  }

  /** Let go of everything read. */
  function stop(): void {
    generation += 1;
    suspended = false;
    read?.cancel();
    read = null;
    store = null;
    plan = null;
    drawn = null;
    cursor = null;
    renderer?.clear();
    panel?.clearCursor();
    status = { kind: "waiting" };
  }

  function begin(): void {
    // What goes wrong in here is the section's alone.
    start().catch((error: unknown) => console.warn("cross-section:", error));
  }

  /** Read the run's section along the line, from the beginning. */
  async function start(): Promise<void> {
    stop();
    // The channel of a run that is over; a retry has its store still.
    if (!source.current) source.end();
    const sequence = generation;
    status = { kind: "opening" };
    showLine();
    render();
    panel?.announce(t("readingData"));
    let opened: SectionStore;
    let painter: SectionRenderer;
    let planned: SectionPlan;
    try {
      opened = await source.open();
      if (sequence !== generation || !view.section) return;
      if (fillsOf(opened).length === 0) throw new SectionError("model", "the store lacks the arrays of a section");
      const cut = clampLine(view.section, opened);
      if (cut.clamped) {
        view.section = cutLine = sectionLine(cut.line);
        deps.syncUrl();
      }
      planned = planSection(view.section, opened);
      if (planned.cells.length === 0) throw new SectionError("outside", "the line lies off the grid");
      painter = ensureRenderer();
      painter.setSection(opened, planned);
      store = opened;
      plan = planned;
      full = true;
      status = { kind: "reading" };
      showLine();
      render();
      draw();
    } catch (error) {
      if (sequence !== generation) return;
      // Diagnostics stay English.
      console.warn("cross-section: not opened:", error instanceof Error ? error.message : error);
      const reason = error instanceof SectionError ? error.reason : "failed";
      stop();
      nothing(reason, reason === "failed");
      return;
    }
    if (aimDue) aim();
    aimDue = false;
    const ids = [...sectionArrays(opened).values()].map(({ id }) => id);
    const reading = source.read(planned, ids, (array, indices, codes) => {
      if (sequence === generation) painter.addCells(array, indices, codes);
    });
    read = reading;
    try {
      await reading.done;
      if (sequence !== generation) return;
      status = { kind: "ready" };
    } catch (error) {
      if (sequence !== generation) return;
      console.warn("cross-section: read stopped:", error instanceof Error ? error.message : error);
      status = { kind: "failed" };
    }
    render();
    panel?.announce(status.kind === "failed" ? t("sectionFailed") : panel.summary());
  }

  /** Take the line away, and the panel with it. */
  function clear(): void {
    view.section = null;
    stop();
    full = false;
    showLine();
    render();
    deps.syncUrl();
    deps.onTool();
  }

  /** What a drag from `a` to `b` would draw; null for an end off the ground. */
  function preview(a: LonLat | null, b: LonLat | null): LinePreview {
    if (!a || !b) return { path: [], label: t("sectionOffMap"), valid: false };
    // Half way round the flat map and more, the shorter way is the other one:
    // the line would leave the pointer.
    if (!isGlobeProjection(map) && Math.abs(b[0] - a[0]) >= 180) return { path: [], label: "", valid: false };
    const refusal = validateLine({ a, b });
    if (refusal === "antipodal") return { path: [], label: t("sectionAntipodal"), valid: false };
    if (refusal) {
      return { path: greatCircle(a, b), label: t("sectionTooShort", { length: formatDistance(MIN_LENGTH) }), valid: false };
    }
    const cut = previewStore ? clampLine({ a, b }, previewStore) : { line: { a, b }, clamped: false };
    const length = formatDistance(distance(cut.line.a, cut.line.b));
    return {
      path: greatCircle(cut.line.a, cut.line.b),
      label: cut.clamped ? t("sectionClamped", { length }) : length,
      valid: true,
    };
  }

  /** The run's store once it is open, which is where a line's limit is read:
   * asked for as the tool is taken up, so that the preview knows it. */
  let previewStore: SectionStore | null = null;

  for (const subscribe of [onLocaleChange, onUnitsChange, onMapPrefsChange]) {
    subscribe(() => {
      render();
      draw();
      readCursor(false);
    });
  }
  onThemeChange(() => draw());

  return {
    clear,
    preview,
    /** The tool has been taken up. */
    armed(): void {
      previewStore = null;
      source.open().then(
        (opened) => {
          previewStore = opened;
        },
        // Drawn anyway: the panel says why there is no section.
        () => {},
      );
    },
    /** A line has been drawn, and goes before a pinned point. */
    drew(a: LonLat, b: LonLat): void {
      const line = sectionLine({ a, b });
      if (validateLine(line)) return;
      view.section = line;
      options.fill = fillForField(view.field);
      aimDue = true;
      deps.closeProbe();
      deps.syncUrl();
      deps.onTool();
      begin();
      escapes.opened(surface);
    },
    /** The run on screen is over; the line may have gone with it. */
    runEnded(): void {
      previewStore = null;
      if (!view.section) {
        stop();
        full = false;
        source.end();
        showLine();
      }
      // What is said of a dataset without a store stands until there is
      // something else to say.
      else if (status.kind !== "none") {
        suspended = true;
        panel?.clearCursor();
      }
      render();
    },
    /** A dataset is open, or the same run's store has changed; the dataset
     * may be one without a run (`manifest` null). */
    runOpened(): void {
      if (!view.section) {
        if (!source.current) source.end();
        return;
      }
      if (store && source.current) {
        suspended = false;
        render();
        draw();
      } else begin();
    },
    /** No run is open after all. */
    runFailed(): void {
      stop();
      source.end();
      if (view.section) nothing("failed", false);
    },
    /** The frame on the map is another, or the levels on it are. */
    frameShown: draw,
    /** The probe panel has taken the slot, or given it back. */
    probeShown(shown: boolean): void {
      if (probeShown === shown) return;
      probeShown = shown;
      panel?.clearCursor();
      render();
      if (!shown && plan) aim();
    },
    setInk: (darkGround: boolean): void => layer.setInk(darkGround),
  };
}

export type CrossSection = ReturnType<typeof createCrossSection>;
