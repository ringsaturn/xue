/**
 * What draws the workbench's planes, wherever it runs: one field, the section
 * along the line and the probe through time drawn from it on a canvas each,
 * and the values the charts beside them are of. `engine.worker.ts` runs it off
 * the main thread; `renderer.ts` on it where a canvas cannot be handed over.
 *
 * A plane is drawn again when what it is of has changed and not otherwise: the
 * probe moving is another picture through time and the same one along the
 * line. At most one is drawn in a frame of the screen.
 */

import { SectionDeriver, roundHeights, type SectionSlice, type TimeColumns } from "../crosssection/derive";
import { DrawBuffers, drawSection } from "../crosssection/draw";
import { SectionField, fillGroup, groupAvailable, type FieldSpec, type SectionArrayId, type SectionGroup } from "../crosssection/field";
import type { SectionBands } from "../crosssection/ramps";
import type { SectionDrawState, SectionDrawn, SectionReading } from "../crosssection/types";
import type { ColumnSurface } from "../sounding/column";
import type { ModelLevel } from "../sounding/profile";

/** The charts of the field on a canvas: `along` the line at a time, and
 * `through` time at the probe. */
export type Plane = "along" | "through";
export const PLANES: readonly Plane[] = ["along", "through"];

/** What both planes are drawn from. */
export interface WorkbenchDraw {
  section: SectionDrawState;
  /** The probe's point of the plan. */
  point: number;
  columns: TimeColumns;
  bands: SectionBands;
}

/** The surface values of a lane. */
export const LANE_ARRAYS = ["tmp2m", "dpt2m", "ugrd10m", "vgrd10m", "prmsl"] as const satisfies readonly SectionArrayId[];
export type LaneArray = (typeof LANE_ARRAYS)[number];

/** The lanes along the line at a time, a value a point; or at a point through
 * time, a value a frame, at the leads given. */
export interface LaneValues {
  /** Seconds, through time; null along the line. */
  leads: number[] | null;
  values: Record<LaneArray, Float32Array>;
}

/** What is read of one place at one time. */
export interface PlaceReading {
  /** At a pressure, as the section's crosshair reads it. */
  reading: SectionReading | null;
  column: { levels: ModelLevel[]; surface: ColumnSurface } | null;
}

/** `epoch` counts sections, and what is said of one carries its own. */
export type EngineRequest =
  | { type: "section"; epoch: number; spec: FieldSpec | null }
  | { type: "cells"; array: string; indices: Uint32Array; codes: Uint8Array }
  | { type: "size"; plane: Plane; width: number; height: number; dpr: number }
  | { type: "draw"; state: WorkbenchDraw }
  | { type: "place"; epoch: number; id: number; point: number; lead: number; pressure: number }
  | { type: "along"; epoch: number; id: number; lead: number }
  | { type: "through"; epoch: number; id: number; point: number };

export type EngineReply =
  | { type: "drawn"; epoch: number; plane: Plane; drawn: SectionDrawn }
  | { type: "place"; id: number; place: PlaceReading }
  | { type: "lanes"; id: number; lanes: LaneValues | null };

/** The least time between two drawings made only because cells arrived. */
const ARRIVAL_MS = 120;

type PlaneCanvas = HTMLCanvasElement | OffscreenCanvas;

interface PlaneView {
  canvas: PlaneCanvas;
  size: { width: number; height: number; dpr: number };
  buffers: DrawBuffers;
  /** What is on the canvas. */
  key: string;
}

export class WorkbenchEngine {
  private epoch = 0;
  private field: SectionField | null = null;
  private deriver: SectionDeriver | null = null;
  private state: WorkbenchDraw | null = null;
  private readonly views: Record<Plane, PlaneView>;
  private scheduled = false;
  private arrival: ReturnType<typeof setTimeout> | null = null;
  private drawnAt = Number.NEGATIVE_INFINITY;
  private ended = false;
  /** The plane drawn last: where both are due the other goes first. */
  private last: Plane = "through";

  constructor(
    canvases: Record<Plane, PlaneCanvas>,
    private readonly reply: (message: EngineReply) => void,
    private readonly nextFrame: (run: () => void) => void,
  ) {
    const view = (canvas: PlaneCanvas): PlaneView => ({ canvas, size: { width: 0, height: 0, dpr: 1 }, buffers: new DrawBuffers(), key: "" });
    this.views = { along: view(canvases.along), through: view(canvases.through) };
  }

  handle(request: EngineRequest): void {
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
      case "size":
        this.views[request.plane].size = { width: request.width, height: request.height, dpr: request.dpr };
        this.schedule();
        break;
      case "draw":
        this.state = request.state;
        this.schedule();
        break;
      case "place": {
        const deriver = request.epoch === this.epoch ? this.deriver : null;
        this.reply({
          type: "place",
          id: request.id,
          place: {
            reading: deriver?.reading(request.lead, request.point, request.pressure) ?? null,
            column: deriver?.columnAt(request.lead, request.point) ?? null,
          },
        });
        break;
      }
      case "along":
        this.reply({ type: "lanes", id: request.id, lanes: request.epoch === this.epoch ? this.along(request.lead) : null });
        break;
      case "through":
        this.reply({ type: "lanes", id: request.id, lanes: request.epoch === this.epoch ? this.through(request.point) : null });
        break;
    }
  }

  end(): void {
    this.ended = true;
    if (this.arrival !== null) clearTimeout(this.arrival);
  }

  private along(lead: number): LaneValues | null {
    const { field } = this;
    const stored = field?.leadOf(lead) ?? null;
    if (!field || stored === null) return null;
    const { points } = field.spec;
    const read = (id: LaneArray): Float32Array => {
      const out = new Float32Array(points).fill(Number.NaN);
      const ready = field.ready(id);
      if (!ready) return out;
      field.blend(id, field.frame(id, stored), out);
      for (let point = 0; point < points; point += 1) if (!ready[point]) out[point] = Number.NaN;
      return out;
    };
    return { leads: null, values: Object.fromEntries(LANE_ARRAYS.map((id) => [id, read(id)])) as LaneValues["values"] };
  }

  private through(point: number): LaneValues | null {
    const { field } = this;
    if (!field || point < 0 || point >= field.spec.points) return null;
    const leads = field.array("tmp")?.offsets ?? field.array("hgt")?.offsets ?? [];
    const one = new Float32Array(1);
    const read = (id: LaneArray): Float32Array => {
      const out = new Float32Array(leads.length).fill(Number.NaN);
      if (field.ready(id)?.[point] !== 1) return out;
      for (const [at, lead] of leads.entries()) {
        field.blend(id, field.frame(id, lead), one, point, point + 1);
        out[at] = one[0]!;
      }
      return out;
    };
    return { leads: [...leads], values: Object.fromEntries(LANE_ARRAYS.map((id) => [id, read(id)])) as LaneValues["values"] };
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
      if (this.ended) return;
      const due = PLANES.filter((plane) => this.views[plane].key !== this.keyOf(plane));
      const plane = due.find((id) => id !== this.last) ?? due[0];
      if (plane === undefined) return;
      this.last = plane;
      this.draw(plane);
      if (due.length > 1) this.schedule();
    });
  }

  /** What a plane's canvas would show: what it is drawn from, spelled out. */
  private keyOf(plane: Plane): string {
    const { field, state } = this;
    const { size } = this.views[plane];
    if (!field || !state) return `${this.epoch}/${size.width}x${size.height}@${size.dpr}/empty`;
    const { lead, ...section } = state.section;
    const own = plane === "along" ? field.leadOf(lead) : [state.point, ...Object.values(state.columns)].join();
    return `${this.epoch}/${size.width}x${size.height}@${size.dpr}/${field.version}/${own}/${JSON.stringify(section)}`;
  }

  private draw(plane: Plane): void {
    const { field, deriver, state } = this;
    const view = this.views[plane];
    const { canvas, size } = view;
    view.key = this.keyOf(plane);
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
    if (!field || !deriver || !state) return;

    const { section, point, columns, bands } = state;
    const has = field.has.bind(field);
    const fill = fillGroup(section.fill);
    const wanted = new Set<SectionGroup>([fill]);
    if (section.lines.wind || section.barbs) wanted.add("wind");
    if (section.lines.zero || section.lines.theta) wanted.add("t");
    const groups = [...wanted].filter((group) => groupAvailable(group, has));
    const slice: SectionSlice | null =
      plane === "along"
        ? deriver.slice(section.lead, section.top, groups)
        : point < field.spec.points
          ? deriver.timeSlice(point, columns, section.top, groups)
          : null;
    if (slice) {
      const levels = field.array("hgt")?.levels ?? field.array("tmp")?.levels ?? [];
      const parts = { fill: groups.includes(fill), wind: groups.includes("wind"), temperature: groups.includes("t") };
      drawSection(context, size, slice, section, bands, levels, parts, view.buffers);
    }
    this.reply({
      type: "drawn",
      epoch: this.epoch,
      plane,
      drawn: {
        complete: field.complete,
        noFrame: plane === "along" && field.leadOf(section.lead) === null,
        heights: slice ? roundHeights(slice, section.units.heightStep) : [],
      },
    });
  }
}
