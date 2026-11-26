/**
 * The workbench and the browser's history. A coordinate that has come to rest
 * is an entry, one for each gesture; how the charts are drawn is not, and
 * replaces the entry there is. Nothing is written while a hand is still
 * moving: the caller commits when it is let go of, and `soon` waits out a run
 * of key presses.
 */

export interface HistoryDeps {
  /** The search the state on the page has. */
  search(): string;
  /** The reader went back or forward: the address is another now. */
  restored(search: string): void;
}

/** How long the arrow keys are still for before what they moved is at rest. */
export const SETTLE_MS = 400;

export function createHistory(deps: HistoryDeps) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  /** What keys at rest are to do next, once they are. */
  let pending: (() => void) | undefined;

  function cancel(): void {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    pending = undefined;
  }

  function write(method: "pushState" | "replaceState"): void {
    const search = deps.search();
    if (search === window.location.search) return;
    window.history[method](null, "", `${window.location.pathname}${search}${window.location.hash}`);
  }

  /** At rest now: what keys moved and had not yet settled settles with it,
   * and does what it was to do. */
  function commit(): void {
    const then = pending;
    cancel();
    write("pushState");
    then?.();
  }

  window.addEventListener("popstate", () => {
    cancel();
    deps.restored(window.location.search);
  });

  return {
    /** A coordinate has come to rest. */
    commit,
    /** A key moved a coordinate: it is at rest once the keys are. */
    soon(then?: () => void): void {
      cancel();
      pending = then;
      timer = setTimeout(commit, SETTLE_MS);
    },
    /** What is drawn changed, and nothing it is drawn of. What keys have moved
     * and not yet settled keeps its own entry. */
    replace(): void {
      if (timer === null) write("replaceState");
    },
    /** A move was taken back before it came to rest. */
    cancel,
  };
}

export type WorkbenchHistory = ReturnType<typeof createHistory>;
