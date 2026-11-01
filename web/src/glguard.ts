/** A browser whose GPU process has wedged hands out WebGL contexts that are
 * lost again the moment they are restored. MapLibre asks for a restore on
 * every loss (`preventDefault`), so the map spins through lose/restore
 * hundreds of times a second and its style never finishes loading. A single
 * loss — a driver reset, a reclaimed background tab — is worth restoring;
 * a burst is not, and nothing the page does will bring that context back. */

/** Losses within `windowMs` past which the context is given up for dead. */
export const CONTEXT_LOSS_LIMIT = 3;
export const CONTEXT_LOSS_WINDOW_MS = 10_000;

/** Counts context losses and says when they have become a loop. */
export class ContextLossCounter {
  private readonly times: number[] = [];
  private dead = false;

  constructor(
    private readonly limit = CONTEXT_LOSS_LIMIT,
    private readonly windowMs = CONTEXT_LOSS_WINDOW_MS,
  ) {}

  /** Record one loss at `now`; true once the context should stay lost. */
  lost(now: number): boolean {
    if (this.dead) return true;
    this.times.push(now);
    while (now - (this.times[0] ?? now) > this.windowMs) this.times.shift();
    this.dead = this.times.length > this.limit;
    return this.dead;
  }
}

/** Watch every canvas inside `container`. Once losses loop, each further
 * loss is stopped before the canvas's own listeners see it: nobody calls
 * `preventDefault`, so the browser does not restore and the loop ends.
 * `onGiveUp` runs once, on the loss that tipped it over. The listener sits
 * on the container in the capture phase so that it runs ahead of the ones
 * MapLibre puts on its canvas. */
export function guardContextLoss(container: HTMLElement, onGiveUp: () => void): void {
  const counter = new ContextLossCounter();
  let told = false;
  container.addEventListener(
    "webglcontextlost",
    (event) => {
      if (!counter.lost(performance.now())) return;
      event.stopImmediatePropagation();
      if (told) return;
      told = true;
      onGiveUp();
    },
    { capture: true },
  );
}
