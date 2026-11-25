// The drag tools of the view tile: over a radar volume a box that opens a
// close-up of the storm under it and a line that stands a vertical section
// along it, and over a run with a column store the line of a cross-section
// (`crosssection/control.ts`). A tool is armed from the view tile and takes
// the next drag on the map — mouse or finger alike, through a transparent
// sheet over the canvas that keeps the drag from panning the map — then
// disarms.

import type { Map as MapLibreMap } from "maplibre-gl";
import { escapes } from "./escapestack";

/** `section` is the volume's, along the straight line the drag draws;
 * `cross` a cross-section's, along the great circle between its ends. */
export type VolumeToolKind = "box" | "section" | "cross";

/** A longitude/latitude pair. */
export type LonLat = [number, number];

/** What a finished drag selected. */
export interface VolumeDrag {
  kind: VolumeToolKind;
  start: LonLat;
  end: LonLat;
}

/** What a `cross` drag shows while it is out: the path, what is said about it
 * beside the path's end (the pointer's, where there is no path), and whether a drag ended here is taken. */
export interface LinePreview {
  path: LonLat[];
  label: string;
  valid: boolean;
}

/** How far from where it was, in screen pixels, a point may come back when it
 * is put on the ground and projected again. Off the globe, or in the sky over
 * a tilted plane, the map answers with the nearest ground there is, which is
 * not where the pointer was. Over the relief the way back is not exact: the
 * point is read off a picture of the terrain's and projected at the height of
 * its DEM, which a slope drawn steep puts some pixels apart. A point that far
 * past the limb is taken for the limb's, which is what the reader is at. */
const ROUND_TRIP_PIXELS = { flat: 4, relief: 24 };

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
  private label: SVGTextElement | null = null;
  private origin: [number, number] | null = null;
  /** The pointer's last place not drawn yet, and the frame that draws it: a
   * preview measures its line, which a pointer outruns. */
  private pending: [number, number] | null = null;
  private frame = 0;
  private readonly surface = { isOpen: () => this.kind !== null, close: () => this.disarm() };

  constructor(
    private readonly map: MapLibreMap,
    private readonly onDrag: (drag: VolumeDrag) => void,
    private readonly onArm: (kind: VolumeToolKind | null) => void,
    /** A `cross` drag's preview from `start` to `end`, either of them null
     * off the ground. */
    private readonly onPreview: (start: LonLat | null, end: LonLat | null) => LinePreview,
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
    this.sheet = sheet;
    escapes.opened(this.surface);
    this.onArm(kind);
  }

  private disarm(notify = true): void {
    if (!this.sheet) return;
    this.sheet.remove();
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.pending = null;
    this.sheet = null;
    this.shape = null;
    this.label = null;
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
    this.shape = document.createElementNS(SVG_NS, this.kind === "box" ? "rect" : this.kind === "cross" ? "polyline" : "line");
    svg.append(this.shape);
    this.label = null;
    if (this.kind === "cross") {
      this.label = document.createElementNS(SVG_NS, "text");
      svg.append(this.label);
    }
    this.draw(this.origin);
  };

  private readonly move = (event: PointerEvent): void => {
    if (!this.origin) return;
    this.pending = this.point(event);
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (this.pending) this.draw(this.pending);
      this.pending = null;
    });
  };

  /** Where a point of the sheet is on the ground, or null where there is
   * none under it. */
  private ground(at: [number, number]): LonLat | null {
    const place = this.map.unproject(at);
    const back = this.map.project(place);
    const tolerance = this.map.getTerrain() ? ROUND_TRIP_PIXELS.relief : ROUND_TRIP_PIXELS.flat;
    return Math.hypot(back.x - at[0], back.y - at[1]) <= tolerance ? [place.lng, place.lat] : null;
  }

  private readonly up = (event: PointerEvent): void => {
    const origin = this.origin;
    const kind = this.kind;
    if (!origin || !kind) return;
    const [x, y] = this.point(event);
    if (!dragSelects(kind, origin[0], origin[1], x, y)) {
      // A tap: still armed, ready for a real drag.
      this.cancel();
      return;
    }
    if (kind === "cross") {
      const from = this.ground(origin);
      const to = this.ground([x, y]);
      // Refused, for what the label said: still armed, like a tap.
      if (!from || !to || !this.onPreview(from, to).valid) {
        this.cancel();
        return;
      }
      this.disarm();
      this.onDrag({ kind, start: from, end: to });
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
    this.pending = null;
    this.sheet?.querySelector("svg")?.replaceChildren();
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
    } else if (this.kind === "cross") {
      const preview = this.onPreview(this.ground(origin), this.ground([x, y]));
      const points = preview.path.map((place) => this.map.project(place));
      shape.setAttribute("points", points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" "));
      shape.toggleAttribute("data-refused", !preview.valid);
      const label = this.label!;
      label.textContent = preview.label;
      // At the end of what is drawn, which is short of the pointer where the
      // line is cut, on the side with room for it.
      const end = points[points.length - 1] ?? { x, y };
      const right = end.x > this.sheet!.clientWidth / 2;
      label.setAttribute("text-anchor", right ? "end" : "start");
      label.setAttribute("x", String(end.x + (right ? -14 : 14)));
      label.setAttribute("y", String(Math.max(20, end.y - 12)));
    } else {
      shape.setAttribute("x1", String(origin[0]));
      shape.setAttribute("y1", String(origin[1]));
      shape.setAttribute("x2", String(x));
      shape.setAttribute("y2", String(y));
    }
  }
}
