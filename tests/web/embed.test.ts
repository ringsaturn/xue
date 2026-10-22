import type { Map as MaplibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";

import { EMBED_READY_EVENT, EmbedHub, installEmbedApi, type EmbedHost, type EmbedOpenCase, type EmbedSource } from "../../web/src/embed";

const GFS: EmbedSource = {
  model: "gfs",
  run: "2026101506",
  runTime: "2026-10-15T06:00:00Z",
  variable: "htsgw",
  caseId: null,
  manifestCrc32: "d7538f9c",
};

/** A host whose answers the test sets, standing in for main.ts. */
function host(initial: { time?: number | null; source?: EmbedSource | null } = {}) {
  const state = { time: initial.time ?? null, source: initial.source ?? null, playing: false };
  const seeks: number[] = [];
  const opens: { id: string; options: EmbedOpenCase }[] = [];
  const h: EmbedHost = {
    map: {} as MaplibreMap,
    time: () => state.time,
    seek: (ms) => {
      seeks.push(ms);
      return state.source === null ? null : ms;
    },
    source: () => state.source,
    playing: () => state.playing,
    openCase: (id, options) => {
      opens.push({ id, options });
      return state.source?.caseId != null;
    },
  };
  return { h, state, seeks, opens };
}

describe("installEmbedApi", () => {
  it("publishes the viewer and announces it", () => {
    const { h } = host();
    const target = new EventTarget() as Window;
    const ready = vi.fn();
    target.addEventListener(EMBED_READY_EVENT, ready);
    const hub = installEmbedApi(h, target);
    expect((target as Window & { xueViewer?: unknown }).xueViewer).toBe(hub.viewer);
    expect(ready).toHaveBeenCalledTimes(1);
    expect(hub.viewer.version).toBe(1);
    expect(hub.viewer.map).toBe(h.map);
    expect(Object.isFrozen(hub.viewer)).toBe(true);
  });
});

describe("EmbedHub", () => {
  it("reads null before the shell is ready", () => {
    const { h } = host();
    const viewer = new EmbedHub(h).viewer;
    expect(viewer.time()).toBeNull();
    expect(viewer.source()).toBeNull();
    expect(viewer.setTime(Date.parse("2026-10-16T00:00:00Z"))).toBeNull();
  });

  it("reports no landing when the shell refuses the frame", () => {
    const h: EmbedHost = {
      map: {} as MaplibreMap,
      time: () => 1,
      seek: () => null,
      source: () => GFS,
      playing: () => false,
      openCase: () => false,
    };
    expect(new EmbedHub(h).viewer.setTime(1000)).toBeNull();
  });

  it("passes seeks through and refuses a time that is not a number", () => {
    const { h, seeks } = host({ source: GFS });
    const viewer = new EmbedHub(h).viewer;
    expect(viewer.setTime(1000)).toBe(1000);
    expect(viewer.setTime(Number.NaN)).toBeNull();
    expect(seeks).toEqual([1000]);
  });

  it("fires time once per valid time", () => {
    const { h, state } = host({ source: GFS });
    const hub = new EmbedHub(h);
    const seen = vi.fn();
    hub.viewer.on("time", seen);
    hub.emitTime(); // not ready: no time yet
    state.time = 1;
    hub.emitTime();
    hub.emitTime(); // a locale or zone change re-renders the same frame
    state.time = 2;
    hub.emitTime();
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("fires source once per model, run, variable and case", () => {
    const { h, state } = host();
    const hub = new EmbedHub(h);
    const seen = vi.fn();
    hub.viewer.on("source", seen);
    hub.emitSource(); // nothing loaded
    state.source = GFS;
    hub.emitSource();
    hub.emitSource(); // a re-tier of the same variable
    state.source = { ...GFS, manifestCrc32: "00000000" };
    hub.emitSource(); // the crc is not part of the identity
    state.source = { ...GFS, variable: "wind10m" };
    hub.emitSource();
    state.source = { ...GFS, variable: "wind10m", caseId: "doksuri-2023", manifestCrc32: null };
    hub.emitSource();
    expect(seen).toHaveBeenCalledTimes(3);
  });

  it("fires source when a rolling window moves on without naming a run", () => {
    const window: EmbedSource = { ...GFS, model: "mrms", run: null, variable: "cref", runTime: "2026-10-15T06:00:00Z" };
    const { h, state } = host({ source: window });
    const hub = new EmbedHub(h);
    const seen = vi.fn();
    hub.viewer.on("source", seen);
    hub.emitSource();
    state.source = { ...window, runTime: "2026-10-15T06:10:00Z" };
    hub.emitSource();
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("stops calling a listener once unsubscribed", () => {
    const { h, state } = host({ source: GFS });
    const hub = new EmbedHub(h);
    const seen = vi.fn();
    const off = hub.viewer.on("time", seen);
    state.time = 1;
    hub.emitTime();
    off();
    state.time = 2;
    hub.emitTime();
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("keeps calling the other listeners when one throws", () => {
    const { h, state } = host({ source: GFS });
    const hub = new EmbedHub(h);
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const after = vi.fn();
    hub.viewer.on("time", () => {
      throw new Error("listener bug");
    });
    hub.viewer.on("time", after);
    state.time = 1;
    expect(() => hub.emitTime()).not.toThrow();
    expect(after).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  it("ignores an unknown event name", () => {
    const { h } = host();
    const viewer = new EmbedHub(h).viewer;
    const off = viewer.on("theme" as "time", () => {});
    expect(off()).toBeUndefined();
  });
});

describe("openCase", () => {
  const CASE: EmbedSource = { ...GFS, caseId: "year-2009", run: "2009010100", runTime: "2009-01-01T00:00:00Z" };

  it("passes a slug through, playing by default", () => {
    const { h, opens } = host({ source: CASE });
    const viewer = new EmbedHub(h).viewer;
    expect(viewer.openCase("year-2010")).toBe(true);
    expect(viewer.openCase("year-2011", { time: Date.parse("2011-03-01T00:00:00Z"), play: false })).toBe(true);
    expect(opens).toEqual([
      { id: "year-2010", options: { time: undefined, play: true } },
      { id: "year-2011", options: { time: Date.parse("2011-03-01T00:00:00Z"), play: false } },
    ]);
  });

  it("refuses an id that is not a slug or a time that is not a number, without asking the shell", () => {
    const { h, opens } = host({ source: CASE });
    const viewer = new EmbedHub(h).viewer;
    expect(viewer.openCase("Year 2010")).toBe(false);
    expect(viewer.openCase("year-2010", { time: Number.NaN })).toBe(false);
    expect(opens).toEqual([]);
  });

  it("answers the shell's refusal off a case, and reports playback", () => {
    const { h, state } = host({ source: GFS });
    const viewer = new EmbedHub(h).viewer;
    expect(viewer.openCase("year-2010")).toBe(false);
    expect(viewer.playing()).toBe(false);
    state.playing = true;
    expect(viewer.playing()).toBe(true);
  });
});
