import { describe, expect, it } from "vitest";
import { CONTEXT_LOSS_LIMIT, CONTEXT_LOSS_WINDOW_MS, ContextLossCounter } from "../../web/src/glguard";

describe("ContextLossCounter", () => {
  it("lets isolated losses be restored", () => {
    const counter = new ContextLossCounter();
    for (let i = 0; i < 10; i++) expect(counter.lost(i * (CONTEXT_LOSS_WINDOW_MS + 1))).toBe(false);
  });

  it("gives up on a burst past the limit, and stays given up", () => {
    const counter = new ContextLossCounter();
    for (let i = 0; i < CONTEXT_LOSS_LIMIT; i++) expect(counter.lost(i)).toBe(false);
    expect(counter.lost(CONTEXT_LOSS_LIMIT)).toBe(true);
    expect(counter.lost(10 * CONTEXT_LOSS_WINDOW_MS)).toBe(true);
  });
});
