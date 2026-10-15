import type { Map as MaplibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";

import { EMBED_READY_EVENT, EmbedHub, installEmbedApi, type EmbedHost, type EmbedSource } from "../../web/src/embed";

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
  const state = { time: initial.time ?? null, source: initial.source ?? null };
  const seeks: number[] = [];
  const h: EmbedHost = {
    map: {} as MaplibreMap,
    time: () => state.time,
    seek: (ms) => {
      seeks.push(ms);
      return state.source === null ? null : ms;
    },
    source: () => state.source,
  };
  return { h, state, seeks };
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
