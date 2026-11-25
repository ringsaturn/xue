/**
 * The run on the page. It stays the run the page opened with until the reader
 * takes the next one: all of it that is drawn is in memory, and a chart being
 * read does not change under the hand. A newer run is offered once its column
 * store is there; the same run published again is taken without a word.
 */

import { fetchLatestPointer, fetchPointedManifest, sameColumns, type ForecastModelId, type LoadedManifest } from "../manifest";

/** How often the pointer is asked for, as the viewer asks. */
export const POLL_MS = 5 * 60_000;

export interface RunHandlers {
  /** The run on the page is another, or its store is. */
  opened(run: LoadedManifest): void;
  /** There is a newer run to take, or no longer. */
  offered(run: LoadedManifest | null): void;
  /** No run could be opened. */
  failed(error: Error): void;
}

export interface RunDeps {
  readonly baseUrl: string;
  readonly model: ForecastModelId;
}

export function createRun(deps: RunDeps, handlers: RunHandlers) {
  let current: LoadedManifest | null = null;
  let newer: LoadedManifest | null = null;
  /** Moves with every fetch and every change of run: the last asked wins. */
  let generation = 0;
  let timer: ReturnType<typeof setInterval> | null = null;

  async function newest(known: LoadedManifest | null): Promise<LoadedManifest | null> {
    const latest = await fetchLatestPointer(deps.baseUrl, deps.model);
    if (known && latest.run === known.latest.run && latest.manifestCrc32 === known.latest.manifestCrc32) return null;
    return fetchPointedManifest(deps.baseUrl, deps.model, latest);
  }

  function show(run: LoadedManifest): void {
    current = run;
    newer = null;
    generation += 1;
    handlers.offered(null);
    handlers.opened(run);
  }

  async function open(): Promise<void> {
    const asked = ++generation;
    try {
      const run = await newest(null);
      if (asked === generation && run) show(run);
    } catch (error) {
      if (asked === generation) handlers.failed(error instanceof Error ? error : new Error(String(error)));
    }
  }

  async function check(): Promise<void> {
    if (!current) return open();
    const asked = ++generation;
    try {
      const run = await newest(newer ?? current);
      if (asked !== generation || !run) return;
      if (run.latest.run === current.latest.run) {
        const same = sameColumns(current.manifest, run.manifest);
        current = run;
        if (!same) show(run);
      } else if (run.manifest.columns) {
        newer = run;
        handlers.offered(run);
      }
    } catch (error) {
      // The run on the page is as good as it was; the next poll asks again.
      console.warn("workbench: pointer not read:", error instanceof Error ? error.message : error);
    }
  }

  function seen(): void {
    if (document.visibilityState === "visible") void check();
  }

  return {
    get current(): LoadedManifest | null {
      return current;
    },
    get newer(): LoadedManifest | null {
      return newer;
    },
    /** Open the newest run and watch for the next. */
    async start(): Promise<void> {
      await open();
      timer ??= setInterval(seen, POLL_MS);
      document.addEventListener("visibilitychange", seen);
    },
    /** Try again what failed. */
    retry: open,
    /** Ask whether there is a newer run. */
    check,
    /** Put the newer run on the page. */
    take(): void {
      if (newer) show(newer);
    },
    end(): void {
      generation += 1;
      if (timer !== null) clearInterval(timer);
      timer = null;
      document.removeEventListener("visibilitychange", seen);
    },
  };
}

export type WorkbenchRun = ReturnType<typeof createRun>;
