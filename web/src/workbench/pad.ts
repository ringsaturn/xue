/**
 * A chart's surface: what a hand or a key on it means, and the rules that show
 * where the page's coordinates are on it. Every chart has a horizontal axis,
 * which is one coordinate of the page's, or none; and the height, or not.
 *
 * A gesture moves one coordinate. It takes the height where it begins on the
 * height's rule and the horizontal one anywhere else, and a key moves whichever
 * it points along. A mouse over a chart with nothing held only reads it.
 *
 * The rules are elements over the canvas, so moving one draws nothing again.
 */

import { pressureY, yPressure } from "../crosssection/chart";
import { element } from "../dom";
import { escapes } from "../escapestack";

/** A coordinate along a chart: places by index, in order from the left. */
export interface PadAxis {
  count: number;
  x(index: number, width: number): number;
  index(x: number, width: number): number;
}

export type PadCoordinate = "axis" | "level";

export interface PadHandlers {
  /** Read here, or (null) no more. */
  hover(index: number | null): void;
  /** The coordinate is being moved to here. */
  move(coordinate: PadCoordinate, value: number): void;
  /** It has been let go of. */
  settle(coordinate: PadCoordinate): void;
  /** The move is taken back. */
  cancel(coordinate: PadCoordinate): void;
  /** A key steps a coordinate: so many places, or to an end (±Infinity). */
  step(coordinate: PadCoordinate, by: number): void;
  resized?(width: number, height: number): void;
}

export interface PadMarks {
  /** Where the axis's coordinate is, or null for not on this chart. */
  index: number | null;
  /** hPa. */
  level: number | null;
  hover: number | null;
  hoverLabel: string;
}

/** Pixels from the height's rule a gesture on it begins within. */
const GRIP = 9;
/** Places a key moves with Shift held. */
const STRIDE = 10;

export class Pad {
  readonly root: HTMLElement;
  width = 0;
  height = 0;
  private axis: PadAxis | null = null;
  /** hPa, or null for a chart without the height. */
  private top: number | null = null;
  private marks: PadMarks = { index: null, level: null, hover: null, hoverLabel: "" };
  private held: { coordinate: PadCoordinate; pointer: number; touch: boolean; index: number | null } | null = null;
  private readonly rule = element("div", "wb-rule");
  private readonly levelRule = element("div", "wb-rule wb-rule-level");
  private readonly hoverRule = element("div", "wb-rule wb-rule-hover");
  private readonly hoverLabel = element("span", "wb-rule-label");
  private readonly surface = { isOpen: () => this.held !== null, close: () => this.takeBack() };

  constructor(
    className: string,
    private readonly handlers: PadHandlers,
  ) {
    this.root = element("div", `wb-pad ${className}`);
    this.root.tabIndex = 0;
    this.root.setAttribute("role", "group");
    this.hoverRule.append(this.hoverLabel);
    this.root.append(this.rule, this.levelRule, this.hoverRule);
    this.root.addEventListener("pointermove", (event) => this.moved(event));
    this.root.addEventListener("pointerdown", (event) => this.pressed(event));
    this.root.addEventListener("pointerup", (event) => this.released(event));
    this.root.addEventListener("pointercancel", (event) => {
      if (this.held?.pointer === event.pointerId) this.takeBack();
    });
    this.root.addEventListener("pointerleave", () => {
      if (!this.held) this.handlers.hover(null);
    });
    this.root.addEventListener("keydown", (event) => this.key(event));
    new ResizeObserver(() => this.measure()).observe(this.root);
    this.place();
  }

  setAxis(axis: PadAxis | null): void {
    this.axis = axis;
    this.place();
  }

  /** The chart's top, hPa, for one that has the height. */
  setTop(top: number | null): void {
    this.top = top;
    this.place();
  }

  setMarks(marks: PadMarks): void {
    this.marks = marks;
    this.place();
  }

  private measure(): void {
    const { clientWidth, clientHeight } = this.root;
    if (clientWidth === 0 || clientHeight === 0) return;
    this.width = clientWidth;
    this.height = clientHeight;
    this.place();
    this.handlers.resized?.(clientWidth, clientHeight);
  }

  private place(): void {
    const { axis, marks, width, height, top } = this;
    const at = (node: HTMLElement, index: number | null): void => {
      const shown = axis !== null && index !== null && index >= 0 && index < axis.count && width > 0;
      const x = shown ? axis.x(index, width) : Number.NaN;
      node.hidden = !(x >= -0.5 && x <= width + 0.5);
      if (!node.hidden) node.style.left = `${x}px`;
      node.dataset.side = x > width / 2 ? "right" : "left";
    };
    at(this.rule, marks.index);
    at(this.hoverRule, marks.hover);
    if (this.hoverLabel.textContent !== marks.hoverLabel) this.hoverLabel.textContent = marks.hoverLabel;
    // The height is kept when the charts' top goes under it; it is not drawn.
    const y = top !== null && marks.level !== null ? pressureY(marks.level, top, height) : Number.NaN;
    this.levelRule.hidden = !(y >= 0 && y <= height);
    if (!this.levelRule.hidden) this.levelRule.style.top = `${y}px`;
  }

  private at(event: PointerEvent): { x: number; y: number } {
    const box = this.root.getBoundingClientRect();
    return {
      x: Math.min(this.width, Math.max(0, event.clientX - box.left)),
      y: Math.min(this.height, Math.max(0, event.clientY - box.top)),
    };
  }

  private onLevel(y: number): boolean {
    const { top, marks, height } = this;
    if (top === null || marks.level === null) return false;
    // A chart with nothing else to move is all the height's.
    return this.axis === null || Math.abs(pressureY(marks.level, top, height) - y) <= GRIP;
  }

  private moved(event: PointerEvent): void {
    const { x, y } = this.at(event);
    const { held, axis } = this;
    if (held) {
      if (held.pointer === event.pointerId) this.drag(x, y);
      return;
    }
    if (event.pointerType !== "mouse") return;
    this.root.style.cursor = this.onLevel(y) ? "ns-resize" : axis ? "crosshair" : "";
    this.handlers.hover(axis ? axis.index(x, this.width) : null);
  }

  private pressed(event: PointerEvent): void {
    if (event.button !== 0 || this.held) return;
    const { x, y } = this.at(event);
    const coordinate: PadCoordinate = this.onLevel(y) ? "level" : "axis";
    if (coordinate === "axis" && !this.axis) return;
    this.root.setPointerCapture(event.pointerId);
    this.held = { coordinate, pointer: event.pointerId, touch: event.pointerType === "touch", index: null };
    escapes.opened(this.surface);
    this.drag(x, y);
  }

  private drag(x: number, y: number): void {
    const { held, axis, top } = this;
    if (!held) return;
    if (held.coordinate === "level") {
      if (top !== null) this.handlers.move("level", yPressure(y, top, this.height));
      return;
    }
    held.index = axis!.index(x, this.width);
    // A finger reads as it goes, and moves the coordinate when it lifts.
    if (held.touch) this.handlers.hover(held.index);
    else {
      this.handlers.hover(null);
      this.handlers.move("axis", held.index);
    }
  }

  private released(event: PointerEvent): void {
    const { held } = this;
    if (!held || held.pointer !== event.pointerId) return;
    this.held = null;
    if (held.touch && held.coordinate === "axis") {
      this.handlers.hover(null);
      if (held.index !== null) this.handlers.move("axis", held.index);
    }
    this.handlers.settle(held.coordinate);
  }

  private takeBack(): void {
    const { held } = this;
    if (!held) return;
    this.held = null;
    if (this.root.hasPointerCapture(held.pointer)) this.root.releasePointerCapture(held.pointer);
    this.handlers.hover(null);
    this.handlers.cancel(held.coordinate);
  }

  private key(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const stride = event.shiftKey ? STRIDE : 1;
    const horizontal = { ArrowLeft: -stride, ArrowRight: stride, Home: Number.NEGATIVE_INFINITY, End: Number.POSITIVE_INFINITY }[event.key];
    const vertical = { ArrowUp: 1, ArrowDown: -1 }[event.key];
    if (horizontal !== undefined && this.axis) this.handlers.step("axis", horizontal);
    else if (vertical !== undefined && this.top !== null) this.handlers.step("level", vertical);
    else return;
    event.preventDefault();
  }
}
