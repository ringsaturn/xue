/**
 * What draws a section, wherever it runs: the samples, the frames derived from
 * them and the canvas. `section.worker.ts` runs it off the main thread;
 * `renderer.ts` runs it on it where a canvas cannot be handed over.
 */

import { SectionDeriver, roundHeights } from "./derive";
import { DrawBuffers, drawSection } from "./draw";
import { SectionField, fillGroup, groupAvailable, type FieldSpec, type SectionGroup } from "./field";
import type { SectionBands } from "./ramps";
import type { SectionDrawState, SectionDrawn, SectionReading } from "./types";

/** What the engine is told. `epoch` counts sections, and what is said of one
 * carries its own, so nothing of the last is taken for the one on screen. */
export type SectionRequest =
  | { type: "section"; epoch: number; spec: FieldSpec | null }
  | { type: "cells"; array: string; indices: Uint32Array; codes: Uint8Array }
  | { type: "resize"; width: number; height: number; dpr: number }
  | { type: "draw"; state: SectionDrawState; bands: SectionBands }
  | { type: "probe"; epoch: number; id: number; point: number; pressure: number };

export type SectionReply =
  | { type: "drawn"; epoch: number; drawn: SectionDrawn }
  | { type: "reading"; id: number; reading: SectionReading | null };

/** The least time between two drawings made only because cells arrived. */
const ARRIVAL_MS = 120;

export class SectionEngine {
  private epoch = 0;
  private field: SectionField | null = null;
  private deriver: SectionDeriver | null = null;
  private state: SectionDrawState | null = null;
  private bands: SectionBands | null = null;
  private size = { width: 0, height: 0, dpr: 1 };
  private readonly buffers = new DrawBuffers();
  private scheduled = false;
  private arrival: ReturnType<typeof setTimeout> | null = null;
  private drawnAt = Number.NEGATIVE_INFINITY;
  private ended = false;

  constructor(
    private readonly canvas: HTMLCanvasElement | OffscreenCanvas,
    private readonly reply: (message: SectionReply) => void,
    private readonly nextFrame: (run: () => void) => void,
  ) {}

  handle(request: SectionRequest): void {
    switch (request.type) {
      case "section":
        this.epoch = request.epoch;
        this.field = request.spec && new SectionField(request.spec);
        this.deriver = this.field && new SectionDeriver(this.field);
        this.schedule();
        break;
      case "cells":
        if (!this.field) break;
        this.field.add(request.array, request.indices, request.codes);
        this.arrived();
        break;
      case "resize":
        this.size = { width: request.width, height: request.height, dpr: request.dpr };
        this.schedule();
        break;
      case "draw":
        this.state = request.state;
        this.bands = request.bands;
        this.schedule();
        break;
      case "probe": {
        const reading =
          request.epoch === this.epoch && this.deriver && this.state
            ? this.deriver.reading(this.state.lead, request.point, request.pressure)
            : null;
        this.reply({ type: "reading", id: request.id, reading });
        break;
      }
    }
  }

  end(): void {
    this.ended = true;
    if (this.arrival !== null) clearTimeout(this.arrival);
  }

  private arrived(): void {
    if (this.scheduled || this.arrival !== null) return;
    const wait = Math.max(0, this.drawnAt + ARRIVAL_MS - performance.now());
    this.arrival = setTimeout(() => {
      this.arrival = null;
      this.schedule();
    }, wait);
  }

  private schedule(): void {
    if (this.scheduled) return;
    this.scheduled = true;
    this.nextFrame(() => {
      this.scheduled = false;
      if (!this.ended) this.draw();
    });
  }

  private draw(): void {
    const { canvas, field, deriver, state, bands, size } = this;
    const width = Math.round(size.width * size.dpr);
    const height = Math.round(size.height * size.dpr);
    if (width < 1 || height < 1) return;
    // Setting a size clears the canvas, so it is set where what replaces the
    // picture is drawn.
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const context = canvas.getContext("2d") as CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null;
    if (!context) return;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, width, height);
    this.drawnAt = performance.now();
    if (!field || !deriver || !state || !bands) return;

    const has = field.has.bind(field);
    const fill = fillGroup(state.fill);
    const wanted = new Set<SectionGroup>([fill]);
    if (state.lines.wind || state.barbs) wanted.add("wind");
    if (state.lines.zero || state.lines.theta) wanted.add("t");
    const groups = [...wanted].filter((group) => groupAvailable(group, has));
    const slice = deriver.slice(state.lead, state.top, groups);
    if (slice) {
      const levels = field.array("hgt")?.levels ?? field.array("tmp")?.levels ?? [];
      const parts = {
        fill: groups.includes(fill),
        wind: groups.includes("wind"),
        temperature: groups.includes("t"),
      };
      drawSection(context, size, slice, state, bands, levels, parts, this.buffers);
    }
    this.reply({
      type: "drawn",
      epoch: this.epoch,
      drawn: {
        complete: field.complete,
        noFrame: field.leadOf(state.lead) === null,
        heights: slice ? roundHeights(slice, state.units.heightStep) : [],
      },
    });
  }
}
