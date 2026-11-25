/**
 * The workbench's planes off the main thread: both canvases handed over first,
 * then the engine's own messages.
 */

import { WorkbenchEngine, type EngineReply, type EngineRequest, type Plane } from "./engine";

const scope = self as unknown as DedicatedWorkerGlobalScope;
let engine: WorkbenchEngine | null = null;

const nextFrame = (run: () => void): void => {
  if (typeof scope.requestAnimationFrame === "function") scope.requestAnimationFrame(run);
  else setTimeout(run, 16);
};

scope.onmessage = (event: MessageEvent<EngineRequest | { type: "canvases"; canvases: Record<Plane, OffscreenCanvas> }>) => {
  const message = event.data;
  if (message.type === "canvases") {
    engine = new WorkbenchEngine(message.canvases, (reply: EngineReply) => scope.postMessage(reply), nextFrame);
    return;
  }
  engine?.handle(message);
};
