/**
 * The section's chart off the main thread: the canvas handed over first, then
 * the engine's own messages. A worker reads no style sheet, so the theme comes
 * with what is to be drawn.
 */

import { SectionEngine, type SectionReply, type SectionRequest } from "./engine";

const scope = self as unknown as DedicatedWorkerGlobalScope;
let engine: SectionEngine | null = null;

const nextFrame = (run: () => void): void => {
  if (typeof scope.requestAnimationFrame === "function") scope.requestAnimationFrame(run);
  else setTimeout(run, 16);
};

scope.onmessage = (event: MessageEvent<SectionRequest | { type: "canvas"; canvas: OffscreenCanvas }>) => {
  const message = event.data;
  if (message.type === "canvas") {
    engine = new SectionEngine(message.canvas, (reply: SectionReply) => scope.postMessage(reply), nextFrame);
    return;
  }
  engine?.handle(message);
};
