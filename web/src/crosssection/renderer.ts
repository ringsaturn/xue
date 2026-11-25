/**
 * The section's chart, from the page's side: a canvas that shows the plot and
 * nothing else, and the engine that draws it. The engine runs in a worker the
 * canvas is handed to, or here where a canvas cannot be handed over.
 */

import { decodeValue } from "../palettes";
import type { SectionEngine, SectionReply, SectionRequest } from "./engine";
import { sectionArrays, type FieldSpec, type SectionArrayId } from "./field";
import { sectionBands } from "./ramps";
import type { SectionDrawState, SectionDrawn, SectionPlan, SectionReading, SectionStore } from "./types";

export { SECTION_ARRAYS, fillAvailable, sectionArrays } from "./field";

export interface SectionHandlers {
  /** After every drawing of the section on screen, those made because more of
   * it arrived too. */
  onDrawn(drawn: SectionDrawn): void;
  /** Nothing more will be drawn on this canvas. */
  onFailed?(message: string): void;
}

/** What the engine reads of a store and a plan. The store's grid is functions
 * and cannot be posted; its codebooks go as the value of every code. */
export function fieldSpec(store: SectionStore, plan: SectionPlan): FieldSpec {
  const arrays = [...sectionArrays(store)].map(([id, array]) => {
    const tables = new Float32Array(array.members.length * 256);
    for (const [member, variable] of array.members.entries()) {
      for (let code = 0; code < 256; code += 1) tables[member * 256 + code] = decodeValue(variable, code) ?? Number.NaN;
    }
    return { id, levels: array.levels, offsets: array.offsets, tables };
  });
  return {
    arrays,
    points: plan.points.length,
    cells: plan.cells.length / 2,
    corners: plan.corners,
    weights: plan.weights,
    bearings: Float64Array.from(plan.points, (point) => point.bearing),
    latitudes: Float64Array.from(plan.points, (point) => point.lat),
  };
}

/** A view that is the whole of a buffer of its own, which can be posted away
 * without taking anything else along. */
function owned<T extends Uint8Array | Uint32Array>(view: T): T {
  return view.byteLength === view.buffer.byteLength ? view : (view.slice() as T);
}

export class SectionRenderer {
  private post: (request: SectionRequest, transfer?: Transferable[]) => void;
  private worker: Worker | null = null;
  private engine: SectionEngine | null = null;
  private epoch = 0;
  private probes = new Map<number, (reading: SectionReading | null) => void>();
  private nextProbe = 1;
  private destroyed = false;
  /** What the section on screen calls each array of the store. */
  private quantities = new Map<string, SectionArrayId>();

  /** A canvas is for one renderer: handed over, it cannot be had back. */
  constructor(
    canvas: HTMLCanvasElement,
    private readonly handlers: SectionHandlers,
  ) {
    if (typeof Worker === "function" && typeof canvas.transferControlToOffscreen === "function") {
      const worker = new Worker(new URL("./section.worker.ts", import.meta.url), { type: "module" });
      worker.onmessage = (event: MessageEvent<SectionReply>) => this.take(event.data);
      worker.onerror = (event) => this.fail(event.message || "worker failed");
      const offscreen = canvas.transferControlToOffscreen();
      worker.postMessage({ type: "canvas", canvas: offscreen }, [offscreen]);
      this.worker = worker;
      this.post = (request, transfer = []) => worker.postMessage(request, transfer);
      return;
    }
    const waiting: SectionRequest[] = [];
    this.post = (request) => void waiting.push(request);
    void import("./engine")
      .then(({ SectionEngine }) => {
        if (this.destroyed) return;
        // Here it would throw at whoever posts, or at the screen's frame: what
        // a worker of its own ends on, it ends on too.
        const guarded = (run: () => void): void => {
          try {
            run();
          } catch (error) {
            this.fail(error instanceof Error ? error.message : String(error));
          }
        };
        const engine = new SectionEngine(
          canvas,
          (reply) => this.take(reply),
          (run) => void requestAnimationFrame(() => guarded(run)),
        );
        this.engine = engine;
        this.post = (request) => guarded(() => engine.handle(request));
        for (const request of waiting) this.post(request);
      })
      .catch((error: unknown) => this.fail(error instanceof Error ? error.message : String(error)));
  }

  /** Start on a section; nothing of the one before is kept. */
  setSection(store: SectionStore, plan: SectionPlan): void {
    this.quantities = new Map([...sectionArrays(store)].map(([id, array]) => [array.id, id]));
    this.begin(fieldSpec(store, plan));
  }

  /** Leave the canvas empty: there is no section to draw. */
  clear(): void {
    this.begin(null);
  }

  private begin(spec: FieldSpec | null): void {
    if (this.destroyed) return;
    this.epoch += 1;
    this.post({ type: "section", epoch: this.epoch, spec });
  }

  /** Give the section some cells of an array, as `source.ts` reads them. The
   * arrays given are the renderer's from here on. */
  addCells(array: string, indices: Uint32Array, codes: Uint8Array): void {
    const quantity = this.quantities.get(array);
    if (this.destroyed || !quantity) return;
    const cells = owned(indices);
    const values = owned(codes);
    this.post({ type: "cells", array: quantity, indices: cells, codes: values }, [...new Set([cells.buffer, values.buffer])] as ArrayBuffer[]);
  }

  resize(cssWidth: number, cssHeight: number, dpr: number): void {
    if (!this.destroyed) this.post({ type: "resize", width: cssWidth, height: cssHeight, dpr });
  }

  /** Show a frame. The last of several before the screen's next is drawn. */
  draw(state: SectionDrawState): void {
    if (!this.destroyed) this.post({ type: "draw", state, bands: sectionBands(state.fill) });
  }

  /** The values of the frame on screen at a point of the plan and a pressure,
   * or null where the section has none. */
  probe(point: number, pressure: number): Promise<SectionReading | null> {
    if (this.destroyed) return Promise.resolve(null);
    return new Promise((resolve) => {
      const id = this.nextProbe;
      this.nextProbe += 1;
      this.probes.set(id, resolve);
      this.post({ type: "probe", epoch: this.epoch, id, point, pressure });
    });
  }

  destroy(): void {
    this.destroyed = true;
    this.worker?.terminate();
    this.engine?.end();
    this.release();
  }

  private take(reply: SectionReply): void {
    if (this.destroyed) return;
    if (reply.type === "reading") {
      this.probes.get(reply.id)?.(reply.reading);
      this.probes.delete(reply.id);
    } else if (reply.epoch === this.epoch) {
      this.handlers.onDrawn(reply.drawn);
    }
  }

  private fail(message: string): void {
    if (this.destroyed) return;
    console.warn("cross-section:", message);
    this.release();
    this.handlers.onFailed?.(message);
  }

  /** Let go of the readings that will not come. */
  private release(): void {
    for (const resolve of this.probes.values()) resolve(null);
    this.probes.clear();
  }
}
