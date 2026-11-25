import type { Map as MaplibreMap } from "maplibre-gl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createCrossSection, type CrossSectionDeps } from "../../web/src/crosssection/control";
import { SectionError, type SectionSource } from "../../web/src/crosssection/source";
import type { SectionArray, SectionDrawState, SectionDrawn, SectionStore } from "../../web/src/crosssection/types";
import { escapes } from "../../web/src/escapestack";
import { gridMapFor, parseGrid } from "../../web/src/gridmap";
import type { ColumnQuantity } from "../../web/src/identity";
import type { ForecastManifest } from "../../web/src/manifest";
import { setUnitOverride } from "../../web/src/units";
import { parseView } from "../../web/src/viewstate";

/** What the page's renderer was asked, in place of the one that draws. */
const renderer = vi.hoisted(() => ({
  draws: [] as unknown[],
  sections: 0,
  cleared: 0,
  cells: 0,
  handlers: null as { onDrawn(drawn: unknown): void } | null,
}));

vi.mock("../../web/src/crosssection/renderer", () => ({
  SectionRenderer: class {
    constructor(_canvas: HTMLCanvasElement, handlers: { onDrawn(drawn: unknown): void }) {
      renderer.handlers = handlers;
    }
    setSection() {
      renderer.sections += 1;
    }
    clear() {
      renderer.cleared += 1;
    }
    addCells() {
      renderer.cells += 1;
    }
    resize() {}
    draw(state: unknown) {
      renderer.draws.push(state);
    }
    probe() {
      return Promise.resolve(null);
    }
    destroy() {}
  },
}));

// For the whole file: a section made by one test hears of the units set before
// the next.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const RUN = Date.UTC(2026, 10, 24, 18);
const HOUR = 3_600_000;

function store(): SectionStore {
  const array = (id: string, levels: number[] | null): [string, SectionArray] => [
    id,
    { id, quantity: id as ColumnQuantity, levels, offsets: [0, 3600], members: [] },
  ];
  return {
    model: "GFS",
    runTime: RUN,
    grid: gridMapFor(parseGrid({ width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25 })),
    width: 1440,
    height: 721,
    blockHeight: 8,
    blockWidth: 8,
    arrays: new Map([
      ...["tmp", "rh", "hgt", "ugrd", "vgrd"].map((id) => array(id, [1000, 850])),
      ...["tmp2m", "dpt2m", "prmsl", "orog", "ugrd10m", "vgrd10m"].map((id) => array(id, null)),
    ]),
  };
}

/** A map that only keeps its sources. */
function fakeMap() {
  const canvas = document.createElement("canvas");
  canvas.tabIndex = 0;
  document.body.append(canvas);
  const sources = new Map<string, { data: unknown; setData(data: unknown): void }>();
  return {
    canvas,
    sources,
    getSource: (id: string) => sources.get(id),
    addSource: (id: string, { data }: { data: unknown }) => {
      const source = {
        data,
        setData(next: unknown) {
          source.data = next;
        },
      };
      sources.set(id, source);
    },
    removeSource: (id: string) => sources.delete(id),
    addLayer: () => {},
    getLayer: () => undefined,
    removeLayer: () => {},
    setPaintProperty: () => {},
    getContainer: () => document.createElement("div"),
    getCenter: () => ({ lng: 130, lat: 38 }),
    getPitch: () => 0,
    getZoom: () => 3,
    getProjection: () => undefined,
    // A degree a pixel, the line low in the window.
    project: ([lon, lat]: [number, number]) => ({ x: 620 + lon, y: 700 - lat }),
    unproject: (at: [number, number] | { x: number; y: number }) => {
      const [x, y] = Array.isArray(at) ? at : [at.x, at.y];
      return { lng: x - 620, lat: 700 - y };
    },
    getCanvas: () => canvas,
    easeTo: vi.fn(),
  };
}

function setup(search = "", manifest: unknown = { columns: {} }) {
  const capsule = document.createElement("div");
  document.body.append(capsule);
  const map = fakeMap();
  const { view } = parseView(search, { field: "prate", lines: null, particles: true, setupLayers: { inflow: true, front: true } });
  const reads: { plan: unknown; resolve: () => void; reject: (error: Error) => void; cancelled: boolean }[] = [];
  const opens = { fail: null as Error | null, count: 0, store };
  const page = { manifest: manifest as ForecastManifest | null, lead: 0 as number | null };
  const columns = () => page.manifest?.columns ?? null;
  let open: unknown = null;
  const source: SectionSource = {
    open: () => {
      opens.count += 1;
      if (opens.fail) return Promise.reject(opens.fail);
      open = columns();
      return Promise.resolve(opens.store());
    },
    get current() {
      return open !== null && open === columns();
    },
    read: (plan) => {
      const entry = { plan, resolve: () => {}, reject: (_: Error) => {}, cancelled: false };
      const done = new Promise<void>((resolve, reject) => {
        entry.resolve = resolve;
        entry.reject = reject;
      });
      reads.push(entry);
      return {
        done,
        cancel: () => {
          entry.cancelled = true;
          entry.reject(new DOMException("cancelled", "AbortError"));
        },
      };
    },
    end: () => {
      open = null;
    },
  };
  const deps: CrossSectionDeps = {
    map: map as unknown as MaplibreMap,
    view,
    source,
    capsule,
    get manifest() {
      return page.manifest;
    },
    modelLabel: "GFS",
    playhead: () => (page.lead === null ? null : RUN + page.lead * HOUR),
    closeProbe: vi.fn(),
    syncUrl: vi.fn(),
    onTool: vi.fn(),
  };
  const section = createCrossSection(deps);
  const panel = () => document.querySelector<HTMLElement>("#section-panel")!;
  return { section, deps, view, page, reads, opens, map, panel };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const drawn = (over: Partial<SectionDrawn> = {}): SectionDrawn => ({ complete: 1, noFrame: false, heights: [], ...over });

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
  Object.assign(renderer, { draws: [], sections: 0, cleared: 0, cells: 0, handlers: null });
});

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The window of a desktop, with the panel's share of it. */
function layOut(): void {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: () => {} }));
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const box = this.id === "section-panel" ? { left: 270, top: 500, right: 1230, bottom: 840 } : { left: 0, top: 0, right: 1500, bottom: 950 };
    return box as DOMRect;
  });
}

const frame = () => new Promise((resolve) => setTimeout(resolve, 40));

describe("a cross-section", () => {
  it("moves the map out from under the panel once for a line, and again when the panel is back", async () => {
    layOut();
    const { section, map, page } = setup();
    section.runOpened();
    section.drew([116.4, 39.9], [139.7, 35.7]);
    await settle();
    await frame();
    expect(map.easeTo).toHaveBeenCalledTimes(1);
    expect(map.easeTo.mock.calls[0]![0]).toMatchObject({ duration: 0, offset: [-1.95, -201].map((value) => expect.closeTo(value, 0)) });
    expect(map.easeTo.mock.calls[0]![0]).not.toHaveProperty("zoom");
    section.runEnded();
    page.manifest = { columns: {} } as never;
    section.runOpened();
    await settle();
    section.frameShown();
    await frame();
    expect(map.easeTo).toHaveBeenCalledTimes(1);
    section.probeShown(true);
    section.probeShown(false);
    await frame();
    expect(map.easeTo).toHaveBeenCalledTimes(2);
  });

  it("redraws in the reader's units", async () => {
    const { section } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    expect((renderer.draws.at(-1) as SectionDrawState).units.wind.interval).toBe(10);
    setUnitOverride("wind", "kt");
    expect((renderer.draws.at(-1) as SectionDrawState).units.wind.interval).toBe(20);
  });

  it("is read along the line, drawn at the playhead, and closed with its line", async () => {
    const { section, deps, view, page, reads, panel, map } = setup();
    page.lead = 1;
    section.runOpened();
    section.drew([116.4, 39.9], [139.7, 35.7]);
    expect(deps.closeProbe).toHaveBeenCalled();
    await settle();
    expect(view.section).toEqual({ a: [116.4, 39.9], b: [139.7, 35.7] });
    expect(renderer.sections).toBe(1);
    expect((renderer.draws.at(-1) as SectionDrawState).lead).toBe(3600);
    expect(panel().hidden).toBe(false);
    expect(panel().dataset.collapsed).toBe("false");
    renderer.handlers!.onDrawn(drawn({ complete: 0.42 }));
    expect(panel().querySelector(".section-notice")!.textContent).toBe("Loading 42%");
    reads[0]!.resolve();
    await settle();
    expect(panel().querySelector(".section-notice")!.textContent).toBe("");
    renderer.handlers!.onDrawn(drawn({ noFrame: true }));
    expect(panel().querySelector(".section-notice")!.textContent).toBe("No data at this time");
    expect(map.sources.size).toBe(1);
    expect(escapes.press()).toBe(true);
    expect(view.section).toBeNull();
    expect(panel().hidden).toBe(true);
    expect(map.sources.size).toBe(0);
  });

  it("shortens a line past the limit, says so, and keeps what is left in the view", async () => {
    const { section, view, panel } = setup();
    section.runOpened();
    section.drew([0, 0], [120, 30]);
    await settle();
    expect(view.section!.a).toEqual([0, 0]);
    expect(view.section!.b[0]).toBeLessThan(100);
    expect(panel().querySelector(".section-notice")!.textContent).toMatch(/Shortened to .* km/);
  });

  it("takes a run's chart off with the run, and reads the next one's", async () => {
    const { section, reads, page, panel } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    expect(reads).toHaveLength(1);
    const canvas = panel().querySelector<HTMLElement>(".section-canvas")!;
    section.runEnded();
    page.manifest = null;
    expect(canvas.style.visibility).toBe("hidden");
    expect(panel().dataset.collapsed).toBe("false");
    const draws = renderer.draws.length;
    section.frameShown();
    expect(renderer.draws).toHaveLength(draws);
    page.manifest = { columns: {} } as never;
    section.runOpened();
    expect(reads[0]!.cancelled).toBe(true);
    await settle();
    expect(reads).toHaveLength(2);
    expect(canvas.style.visibility).toBe("visible");
  });

  it("has its chart back as it was where the store is the same", async () => {
    const { section, reads, page, panel } = setup("?section=0.00,0.00,30.00,120.00");
    section.runOpened();
    await settle();
    const manifest = page.manifest;
    section.runEnded();
    page.manifest = null;
    page.manifest = { ...manifest } as never;
    section.runOpened();
    await settle();
    expect(reads).toHaveLength(1);
    expect(reads[0]!.cancelled).toBe(false);
    expect(panel().querySelector<HTMLElement>(".section-canvas")!.style.visibility).toBe("visible");
    // What was said of the line is said of it still, by another run's too.
    expect(panel().querySelector(".section-notice")!.textContent).toMatch(/Shortened/);
    page.manifest = { columns: {} } as never;
    section.runEnded();
    section.runOpened();
    await settle();
    expect(reads).toHaveLength(2);
    expect(panel().querySelector(".section-notice")!.textContent).toMatch(/Shortened/);
  });

  it("stays in one row until there is a chart, and when no run opens", async () => {
    const { section, panel, page } = setup("?section=39.90,116.40,35.70,139.70");
    section.runEnded();
    expect(panel().dataset.collapsed).toBe("true");
    section.runOpened();
    await settle();
    expect(panel().dataset.collapsed).toBe("false");
    section.runEnded();
    page.manifest = null;
    section.runFailed();
    expect(panel().dataset.collapsed).toBe("true");
    expect(panel().querySelector<HTMLElement>(".section-retry")!.hidden).toBe(true);
  });

  it("is off the grid where nothing of the line is on it", async () => {
    const { section, panel, reads, opens } = setup("?section=39.90,116.40,35.70,139.70");
    opens.store = () => ({ ...store(), grid: gridMapFor(parseGrid({ width: 40, height: 30, firstLongitude: 0, firstLatitude: 50, longitudeStep: 0.25, latitudeStep: -0.25 })), width: 40, height: 30 });
    section.runOpened();
    await settle();
    expect(reads).toHaveLength(0);
    expect(panel().querySelector(".section-notice")!.textContent).toBe("Outside this dataset's grid");
  });

  it("says aloud that it reads and that it has, not how far it is", async () => {
    const { section, reads, panel } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    const spoken = panel().querySelector<HTMLElement>('[role="status"]')!;
    expect(spoken.textContent).toBe("Loading data");
    renderer.handlers!.onDrawn(drawn({ complete: 0.42 }));
    expect(spoken.textContent).toBe("Loading data");
    reads[0]!.resolve();
    await settle();
    expect(spoken.textContent).toMatch(/^Vertical section · A 39\.90°N/);
  });

  it("gives the focus back to the map when its own button closes it", async () => {
    const { section, panel, map } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    panel().querySelector<HTMLButtonElement>(".section-close")!.click();
    expect(document.activeElement).toBe(map.canvas);
  });

  it("keeps the line over a dataset without a store, in one row that says why", async () => {
    const { section, opens, view, map, panel } = setup("?section=39.90,116.40,35.70,139.70");
    opens.fail = new SectionError("model", "none");
    section.runOpened();
    await settle();
    expect(view.section).not.toBeNull();
    expect(panel().dataset.collapsed).toBe("true");
    expect(panel().querySelector(".section-notice")!.textContent).toBe("No vertical section for GFS");
    expect(panel().querySelector<HTMLElement>(".section-retry")!.hidden).toBe(true);
    const line = (map.sources.get("cross-section")!.data as { features: { properties: { live: boolean } }[] }).features[0]!;
    expect(line.properties.live).toBe(false);
    opens.fail = null;
    // Nothing flashes while the dataset is opened again.
    section.runEnded();
    expect(panel().dataset.collapsed).toBe("true");
    expect(panel().querySelector(".section-notice")!.textContent).toBe("No vertical section for GFS");
    section.runOpened();
    await settle();
    expect(panel().dataset.collapsed).toBe("false");
  });

  it("asks again for a read that failed, and keeps what had arrived meanwhile", async () => {
    const { section, reads, panel } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    reads[0]!.reject(new Error("gone"));
    await settle();
    expect(panel().dataset.collapsed).toBe("false");
    const retry = panel().querySelector<HTMLButtonElement>(".section-retry")!;
    expect(retry.hidden).toBe(false);
    retry.click();
    await settle();
    expect(reads).toHaveLength(2);
    expect(retry.hidden).toBe(true);
  });

  it("steps aside for a pinned point, drawing nothing meanwhile", async () => {
    const { section, panel, page } = setup("?section=39.90,116.40,35.70,139.70");
    section.runOpened();
    await settle();
    section.probeShown(true);
    expect(panel().hidden).toBe(true);
    const draws = renderer.draws.length;
    page.lead = 1;
    section.frameShown();
    expect(renderer.draws).toHaveLength(draws);
    section.probeShown(false);
    expect(panel().hidden).toBe(false);
    expect((renderer.draws.at(-1) as SectionDrawState).lead).toBe(3600);
  });

  it("drops a line no section is drawn along", () => {
    const { view, deps } = setup("?section=39.90,116.40,39.90,116.50");
    expect(view.section).toBeNull();
    expect(deps.syncUrl).toHaveBeenCalled();
  });

  it("says in the preview why a drag is refused", () => {
    const { section } = setup();
    expect(section.preview([0, 0], null)).toMatchObject({ valid: false, label: "Not on the map" });
    expect(section.preview([0, 0], [1, 0])).toMatchObject({ valid: false, label: "Too short: at least 200 km" });
    expect(section.preview([0, 10], [180, -10])).toMatchObject({ valid: false, path: [] });
    // Past half way round the flat map the line would go the other way.
    expect(section.preview([-100, 10], [100, 20])).toEqual({ path: [], label: "", valid: false });
    const taken = section.preview([0, 0], [20, 0]);
    expect(taken.valid).toBe(true);
    expect(taken.label).toBe("2,226 km");
    expect(taken.path.length).toBeGreaterThan(2);
  });
});
