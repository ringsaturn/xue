/**
 * The workbench's engine, from the page's side: it runs in a worker the
 * planes' canvases are handed to, or here where they cannot be.
 */

import type { SectionArrayId, FieldSpec } from "../crosssection/field";
import { sectionArrays } from "../crosssection/field";
import { sectionBands } from "../crosssection/ramps";
import { fieldSpec, owned } from "../crosssection/renderer";
import type { SectionDrawn, SectionPlan, SectionStore } from "../crosssection/types";
import type { EngineReply, EngineRequest, LaneValues, PlaceReading, Plane, WorkbenchDraw, WorkbenchEngine } from "./engine";

export interface RendererHandlers {
  onDrawn(plane: Plane, drawn: SectionDrawn): void;
  /** Nothing more will be drawn on these canvases. */
  onFailed(message: string): void;
}

const NOTHING: PlaceReading = { reading: null, column: null };

export class WorkbenchRenderer {
  private post: (request: EngineRequest, transfer?: Transferable[]) => void;
  private worker: Worker | null = null;
  private engine: WorkbenchEngine | null = null;
  private epoch = 0;
  private nextId = 1;
  private destroyed = false;
  private places = new Map<number, (place: PlaceReading) => void>();
  private lanes = new Map<number, (lanes: LaneValues | null) => void>();
  /** What the section on screen calls each array of the store. */
  private quantities = new Map<string, SectionArrayId>();

  /** The canvases are this renderer's: handed over, they cannot be had back. */
  constructor(
    canvases: Record<Plane, HTMLCanvasElement>,
    private readonly handlers: RendererHandlers,
  ) {
    if (typeof Worker === "function" && typeof canvases.along.transferControlToOffscreen === "function") {
      const worker = new Worker(new URL("./engine.worker.ts", import.meta.url), { type: "module" });
      worker.onmessage = (event: MessageEvent<EngineReply>) => this.take(event.data);
      worker.onerror = (event) => this.fail(event.message || "worker failed");
      const along = canvases.along.transferControlToOffscreen();
      const through = canvases.through.transferControlToOffscreen();
      worker.postMessage({ type: "canvases", canvases: { along, through } }, [along, through]);
      this.worker = worker;
      this.post = (request, transfer = []) => worker.postMessage(request, transfer);
      return;
    }
    const waiting: EngineRequest[] = [];
    this.post = (request) => void waiting.push(request);
    void import("./engine")
      .then(({ WorkbenchEngine }) => {
        if (this.destroyed) return;
        // What a worker of its own ends on, this ends on too.
        const guarded = (run: () => void): void => {
          if (this.destroyed) return;
          try {
            run();
          } catch (error) {
            this.fail(error instanceof Error ? error.message : String(error));
          }
        };
        const engine = new WorkbenchEngine(
          canvases,
          (reply) => this.take(reply),
          (run) => void requestAnimationFrame(() => guarded(run)),
        );
        this.engine = engine;
        this.post = (request) => guarded(() => engine.handle(request));
        for (const request of waiting) this.post(request);
      })
      .catch((error: unknown) => this.fail(error instanceof Error ? error.message : String(error)));
  }

  setSection(store: SectionStore, plan: SectionPlan): void {
    this.quantities = new Map([...sectionArrays(store)].map(([id, array]) => [array.id, id]));
    this.begin(fieldSpec(store, plan));
  }

  clear(): void {
    this.begin(null);
  }

  private begin(spec: FieldSpec | null): void {
    if (this.destroyed) return;
    this.epoch += 1;
    this.release();
    this.post({ type: "section", epoch: this.epoch, spec });
  }

  addCells(array: string, indices: Uint32Array, codes: Uint8Array): void {
    const quantity = this.quantities.get(array);
    if (this.destroyed || !quantity) return;
    const cells = owned(indices);
    const values = owned(codes);
    this.post({ type: "cells", array: quantity, indices: cells, codes: values }, [...new Set([cells.buffer, values.buffer])] as ArrayBuffer[]);
  }

  size(plane: Plane, width: number, height: number, dpr: number): void {
    if (!this.destroyed) this.post({ type: "size", plane, width, height, dpr });
  }

  draw(state: Omit<WorkbenchDraw, "bands">): void {
    if (!this.destroyed) this.post({ type: "draw", state: { ...state, bands: sectionBands(state.section.fill) } });
  }

  /** The values at a place and time. */
  place(point: number, lead: number, pressure: number): Promise<PlaceReading> {
    if (this.destroyed) return Promise.resolve(NOTHING);
    return new Promise((resolve) => {
      const id = this.nextId++;
      this.places.set(id, resolve);
      this.post({ type: "place", epoch: this.epoch, id, point, lead, pressure });
    });
  }

  along(lead: number): Promise<LaneValues | null> {
    return this.lane((id) => ({ type: "along", epoch: this.epoch, id, lead }));
  }

  through(point: number): Promise<LaneValues | null> {
    return this.lane((id) => ({ type: "through", epoch: this.epoch, id, point }));
  }

  private lane(request: (id: number) => EngineRequest): Promise<LaneValues | null> {
    if (this.destroyed) return Promise.resolve(null);
    return new Promise((resolve) => {
      const id = this.nextId++;
      this.lanes.set(id, resolve);
      this.post(request(id));
    });
  }

  destroy(): void {
    this.destroyed = true;
    this.worker?.terminate();
    this.engine?.end();
    this.release();
  }

  private take(reply: EngineReply): void {
    if (this.destroyed) return;
    if (reply.type === "place") {
      this.places.get(reply.id)?.(reply.place);
      this.places.delete(reply.id);
    } else if (reply.type === "lanes") {
      this.lanes.get(reply.id)?.(reply.lanes);
      this.lanes.delete(reply.id);
    } else if (reply.epoch === this.epoch) this.handlers.onDrawn(reply.plane, reply.drawn);
  }

  private fail(message: string): void {
    if (this.destroyed) return;
    console.warn("workbench:", message);
    this.destroyed = true;
    this.worker?.terminate();
    this.release();
    this.handlers.onFailed(message);
  }

  /** Let go of the readings that will not come. */
  private release(): void {
    for (const resolve of this.places.values()) resolve(NOTHING);
    for (const resolve of this.lanes.values()) resolve(null);
    this.places.clear();
    this.lanes.clear();
  }
}
