// The radar volume's two drag tools: a box that opens a close-up of the
// storm under it, and a line that stands a vertical section along it. A
// tool is armed from the view tile and takes the next drag on the map —
// mouse or finger alike, through a transparent sheet over the canvas that
// keeps the drag from panning the map — then disarms.

import type { Map as MapLibreMap } from "maplibre-gl";

export type VolumeToolKind = "box" | "section";

/** A longitude/latitude pair. */
export type LonLat = [number, number];

/** What a finished drag selected. */
export interface VolumeDrag {
  kind: VolumeToolKind;
  start: LonLat;
  end: LonLat;
}

/** Below this many screen pixels a drag is a tap, and selects nothing. */
export const MIN_DRAG_PIXELS = 12;

/** The box two corners span, west to east and south to north. */
export function boxBounds(a: LonLat, b: LonLat): { west: number; east: number; south: number; north: number } {
  return {
    west: Math.min(a[0], b[0]),
    east: Math.max(a[0], b[0]),
    south: Math.min(a[1], b[1]),
    north: Math.max(a[1], b[1]),
  };
}

/** Whether a drag from `(x0, y0)` to `(x1, y1)` on screen was long enough
 * to mean a selection: both sides for a box, the length for a line. */
export function dragSelects(kind: VolumeToolKind, x0: number, y0: number, x1: number, y1: number): boolean {
  const dx = Math.abs(x1 - x0);
  const dy = Math.abs(y1 - y0);
  return kind === "box" ? dx >= MIN_DRAG_PIXELS && dy >= MIN_DRAG_PIXELS : Math.hypot(dx, dy) >= MIN_DRAG_PIXELS;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export class VolumeTool {
  private sheet: HTMLDivElement | null = null;
  private shape: SVGElement | null = null;
  private kind: VolumeToolKind | null = null;
  private origin: [number, number] | null = null;

  constructor(
    private readonly map: MapLibreMap,
    private readonly onDrag: (drag: VolumeDrag) => void,
    private readonly onArm: (kind: VolumeToolKind | null) => void,
  ) {}

  get armed(): VolumeToolKind | null {
    return this.kind;
  }

  /** Take the next drag as `kind`, or stop waiting for one. */
  arm(kind: VolumeToolKind | null): void {
    this.disarm(false);
    if (!kind) {
      this.onArm(null);
      return;
    }
    this.kind = kind;
    const sheet = document.createElement("div");
    sheet.className = "volume-tool-sheet";
    sheet.dataset.kind = kind;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "volume-tool-shape");
    sheet.append(svg);
    sheet.addEventListener("pointerdown", this.down);
    sheet.addEventListener("pointermove", this.move);
    sheet.addEventListener("pointerup", this.up);
    sheet.addEventListener("pointercancel", this.cancel);
    this.map.getContainer().append(sheet);
    window.addEventListener("keydown", this.key);
    this.sheet = sheet;
    this.onArm(kind);
  }

  private disarm(notify = true): void {
    if (!this.sheet) return;
    this.sheet.remove();
    window.removeEventListener("keydown", this.key);
    this.sheet = null;
    this.shape = null;
    this.origin = null;
    this.kind = null;
    if (notify) this.onArm(null);
  }

  private point(event: PointerEvent): [number, number] {
    const rect = this.sheet!.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  private readonly down = (event: PointerEvent): void => {
    if (!this.sheet || !this.kind) return;
    event.preventDefault();
    this.sheet.setPointerCapture(event.pointerId);
    this.origin = this.point(event);
    const svg = this.sheet.querySelector("svg")!;
    svg.replaceChildren();
    this.shape = document.createElementNS(SVG_NS, this.kind === "box" ? "rect" : "line");
    svg.append(this.shape);
    this.draw(this.origin);
  };

  private readonly move = (event: PointerEvent): void => {
    if (!this.origin) return;
    this.draw(this.point(event));
  };

  private readonly up = (event: PointerEvent): void => {
    const origin = this.origin;
    const kind = this.kind;
    if (!origin || !kind) return;
    const [x, y] = this.point(event);
    if (!dragSelects(kind, origin[0], origin[1], x, y)) {
      // A tap: still armed, ready for a real drag.
      this.origin = null;
      this.sheet?.querySelector("svg")?.replaceChildren();
      return;
    }
    // Never inside a render: unproject reads the terrain's framebuffer.
    const start = this.map.unproject(origin);
    const end = this.map.unproject([x, y]);
    this.disarm();
    this.onDrag({ kind, start: [start.lng, start.lat], end: [end.lng, end.lat] });
  };

  private readonly cancel = (): void => {
    this.origin = null;
    this.sheet?.querySelector("svg")?.replaceChildren();
  };

  private readonly key = (event: KeyboardEvent): void => {
    if (event.key === "Escape") this.disarm();
  };

  private draw([x, y]: [number, number]): void {
    const origin = this.origin;
    const shape = this.shape;
    if (!origin || !shape) return;
    if (this.kind === "box") {
      shape.setAttribute("x", String(Math.min(origin[0], x)));
      shape.setAttribute("y", String(Math.min(origin[1], y)));
      shape.setAttribute("width", String(Math.abs(x - origin[0])));
      shape.setAttribute("height", String(Math.abs(y - origin[1])));
    } else {
      shape.setAttribute("x1", String(origin[0]));
      shape.setAttribute("y1", String(origin[1]));
      shape.setAttribute("x2", String(x));
      shape.setAttribute("y2", String(y));
    }
  }
}
