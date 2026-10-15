/** The embed API: `window.xueViewer`, for a page that builds this shell and
 * draws its own layers over the map in step with playback. Always installed;
 * `docs/contribution/frontend.md` ("Embedding") is the contract.
 *
 * Kept apart from main.ts so it is testable without a map: main.ts hands
 * over getters and calls `emitTime` / `emitSource` where the playhead and the
 * dataset change, and this module owns the listeners and the dedupe. */

import type { Map as MaplibreMap } from "maplibre-gl";

export const EMBED_API_VERSION = 1;
export const EMBED_READY_EVENT = "xue:ready";

export type EmbedEvent = "time" | "source";

/** The dataset on screen. `model` is the source id (`gfs`, `ecmwf`, …);
 * `run` the cycle (`YYYYMMDDHH`), null where a view has none to name;
 * `manifestCrc32` the live pointer's, null for a case. */
export interface EmbedSource {
  model: string;
  run: string | null;
  runTime: string;
  variable: string;
  caseId: string | null;
  manifestCrc32: string | null;
}

export interface XueViewer {
  readonly version: typeof EMBED_API_VERSION;
  /** Outside the version promise: it follows MapLibre's own API. */
  readonly map: MaplibreMap;
  time(): number | null;
  setTime(ms: number): number | null;
  source(): EmbedSource | null;
  on(event: EmbedEvent, callback: () => void): () => void;
}

/** What main.ts answers. Each returns null until the shell is ready and a
 * dataset is loaded; `seek` clamps to the axis and stops playback. */
export interface EmbedHost {
  map: MaplibreMap;
  time(): number | null;
  seek(ms: number): number | null;
  source(): EmbedSource | null;
}

/** What makes a dataset another one. `runTime` covers a rolling window,
 * which names no run but moves on with every round. */
function sourceKey(source: EmbedSource): string {
  return [source.model, source.run ?? "", source.runTime, source.variable, source.caseId ?? ""].join("\u0000");
}

export class EmbedHub {
  readonly viewer: XueViewer;
  private readonly listeners: Record<EmbedEvent, Set<() => void>> = { time: new Set(), source: new Set() };
  private lastTime: number | null = null;
  private lastSource: string | null = null;

  constructor(private readonly host: EmbedHost) {
    const hub = this;
    this.viewer = Object.freeze({
      version: EMBED_API_VERSION,
      map: host.map,
      time: () => host.time(),
      setTime: (ms: number) => (Number.isFinite(ms) ? host.seek(ms) : null),
      source: () => host.source(),
      on(event: EmbedEvent, callback: () => void): () => void {
        const set = hub.listeners[event];
        if (!set || typeof callback !== "function") return () => {};
        set.add(callback);
        return () => set.delete(callback);
      },
    });
  }

  /** The playhead's frame changed; repeated calls at one valid time (a
   * locale or zone change re-renders the readout) stay silent. */
  emitTime(): void {
    const time = this.host.time();
    if (time === null || time === this.lastTime) return;
    this.lastTime = time;
    this.fire("time");
  }

  /** The dataset changed (model, run or its run time, variable, case); a
   * re-tier of the same variable stays silent. */
  emitSource(): void {
    const source = this.host.source();
    if (source === null) return;
    const key = sourceKey(source);
    if (key === this.lastSource) return;
    this.lastSource = key;
    this.fire("source");
  }

  private fire(event: EmbedEvent): void {
    for (const callback of [...this.listeners[event]]) {
      try {
        callback();
      } catch (error) {
        console.error(`xueViewer ${event} listener failed`, error);
      }
    }
  }
}

/** Publish the viewer on `target` and announce it. */
export function installEmbedApi(host: EmbedHost, target: Window = window): EmbedHub {
  const hub = new EmbedHub(host);
  (target as Window & { xueViewer?: XueViewer }).xueViewer = hub.viewer;
  target.dispatchEvent(new Event(EMBED_READY_EVENT));
  return hub;
}
