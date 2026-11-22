import { captureNextRender, drawCaption, type GifHost } from "./gifexport";

/**
 * The frame on screen saved as a PNG: the map's canvas at its own pixels,
 * with the caption and credits burnt in as the GIF has them, and the legend
 * painted where it sits on screen (a still is read slowly, and the key is
 * what it is read by).
 */

/** One colour stop of a CSS gradient: the colour and its place along the
 * line from 0 to 1, null where the stylesheet left it to be spread. */
export interface GradientStop {
  color: string;
  at: number | null;
}

export interface ParsedGradient {
  /** The direction in CSS degrees: 180 runs top to bottom. */
  angle: number;
  stops: GradientStop[];
}

/** `s` split at the commas outside parentheses. */
function topLevelParts(s: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < s.length; i += 1) {
    const c = s[i];
    if (c === "(") depth += 1;
    else if (c === ")") depth -= 1;
    else if (c === "," && depth === 0) {
      parts.push(s.slice(start, i).trim());
      start = i + 1;
    }
  }
  parts.push(s.slice(start).trim());
  return parts.filter(Boolean);
}

const SIDE_ANGLES: Record<string, number> = { top: 0, right: 90, bottom: 180, left: 270 };

/** The first `linear-gradient(…)` of a computed `background-image`, or null.
 * Lengths other than percentages are spread like unplaced stops: the legend's
 * ramps are written in percentages or not at all. */
export function parseLinearGradient(image: string): ParsedGradient | null {
  const open = image.indexOf("linear-gradient(");
  if (open < 0) return null;
  let depth = 0;
  let end = -1;
  for (let i = open + "linear-gradient".length; i < image.length; i += 1) {
    if (image[i] === "(") depth += 1;
    else if (image[i] === ")") {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end < 0) return null;
  const parts = topLevelParts(image.slice(open + "linear-gradient(".length, end));
  let angle = 180;
  const head = parts[0] ?? "";
  if (/^to\s/.test(head)) {
    const sides = head.slice(3).trim().split(/\s+/);
    const values = sides.map((side) => SIDE_ANGLES[side]).filter((value): value is number => value !== undefined);
    if (values.length > 0) angle = values.length === 1 ? values[0]! : (values[0]! + values[1]!) / 2;
    parts.shift();
  } else if (/^-?[\d.]+deg$/.test(head)) {
    angle = Number.parseFloat(head);
    parts.shift();
  }
  const stops: GradientStop[] = [];
  for (const part of parts) {
    // A stop is a colour, then up to two positions: `red 10% 20%` is two.
    const match = /^(.*?)((?:\s+-?[\d.]+(?:%|px)?)*)$/.exec(part);
    if (!match) continue;
    const color = match[1]!.trim();
    const positions = match[2]!.trim().split(/\s+/).filter(Boolean);
    if (positions.length === 0) stops.push({ color, at: null });
    for (const position of positions) {
      stops.push({ color, at: position.endsWith("%") ? Number.parseFloat(position) / 100 : null });
    }
  }
  if (stops.length === 0) return null;
  return { angle, stops: spreadStops(stops) };
}

/** Place the unplaced stops the way CSS does: the ends at 0 and 1, the rest
 * evenly between their placed neighbours, and none before the one ahead. */
function spreadStops(stops: GradientStop[]): GradientStop[] {
  const placed = stops.map((stop) => stop.at);
  if (placed[0] === null) placed[0] = 0;
  if (placed[placed.length - 1] === null) placed[placed.length - 1] = 1;
  let last = 0;
  for (let i = 0; i < placed.length; i += 1) {
    if (placed[i] !== null) {
      placed[i] = Math.max(placed[i]!, last);
      last = placed[i]!;
      continue;
    }
    let next = i + 1;
    while (placed[next] === null) next += 1;
    const from = placed[i - 1]!;
    const to = Math.max(placed[next]!, from);
    const steps = next - i + 1;
    for (let k = i; k < next; k += 1) placed[k] = from + ((to - from) * (k - i + 1)) / steps;
    i = next - 1;
  }
  return stops.map((stop, i) => ({ color: stop.color, at: Math.min(1, Math.max(0, placed[i]!)) }));
}

/** The halo under a label's ink: dark under light ink, light under dark. */
function haloFor(ink: string): string {
  const [r = 0, g = 0, b = 0] = (ink.match(/[\d.]+/g) ?? []).map(Number);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 128 ? "rgba(0, 0, 0, 0.6)" : "rgba(255, 255, 255, 0.75)";
}

function visible(element: Element): boolean {
  const style = getComputedStyle(element);
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0;
}

/** Paint the legend's boxes and words onto `context`, where they sit over the
 * map on screen: the fills (a ramp, a swatch), the borders and the text. Read
 * off the laid-out page rather than drawn again, so whatever key the field
 * carries comes out as it reads. */
export function paintLegend(context: CanvasRenderingContext2D, legend: HTMLElement, mapBox: DOMRect): void {
  if (!visible(legend) || legend.hidden) return;
  const scaleX = context.canvas.width / mapBox.width;
  const scaleY = context.canvas.height / mapBox.height;
  const toCanvas = (rect: DOMRect) => ({
    x: (rect.left - mapBox.left) * scaleX,
    y: (rect.top - mapBox.top) * scaleY,
    w: rect.width * scaleX,
    h: rect.height * scaleY,
  });
  context.save();
  const elements = [legend, ...legend.querySelectorAll<HTMLElement>("*")];
  for (const element of elements) {
    if (element.closest("[hidden]") || !visible(element)) continue;
    const rect = element.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(element);
    const box = toCanvas(rect);
    const radius = Math.min(box.w, box.h) / 2;
    const corner = Math.min(radius, (Number.parseFloat(style.borderTopLeftRadius) || 0) * scaleX);
    const gradient = parseLinearGradient(style.backgroundImage);
    const filled = gradient !== null || !/rgba\(\s*0,\s*0,\s*0,\s*0\s*\)|transparent/.test(style.backgroundColor);
    if (filled) {
      context.beginPath();
      context.roundRect(box.x, box.y, box.w, box.h, corner);
      if (gradient) {
        const rad = (gradient.angle * Math.PI) / 180;
        const dx = Math.sin(rad);
        const dy = -Math.cos(rad);
        const half = (Math.abs(box.w * dx) + Math.abs(box.h * dy)) / 2;
        const cx = box.x + box.w / 2;
        const cy = box.y + box.h / 2;
        const fill = context.createLinearGradient(cx - dx * half, cy - dy * half, cx + dx * half, cy + dy * half);
        for (const stop of gradient.stops) {
          try {
            fill.addColorStop(stop.at ?? 0, stop.color);
          } catch {
            // A colour the canvas cannot read is left out of the ramp.
          }
        }
        context.fillStyle = fill;
      } else {
        context.fillStyle = style.backgroundColor;
      }
      context.fill();
      const border = Number.parseFloat(style.borderTopWidth) || 0;
      if (border > 0 && style.borderTopStyle !== "none") {
        context.lineWidth = Math.max(1, border * scaleX);
        context.strokeStyle = style.borderTopColor;
        context.stroke();
      }
    }
    for (const node of element.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE) continue;
      const words = (node.textContent ?? "").replace(/\s+/g, " ").trim();
      if (!words) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const textBox = toCanvas(range.getBoundingClientRect());
      if (textBox.w === 0) continue;
      const size = (Number.parseFloat(style.fontSize) || 12) * scaleY;
      context.font = `${style.fontWeight} ${size}px ${style.fontFamily}`;
      context.fillStyle = style.color;
      context.textBaseline = "middle";
      context.textAlign = "left";
      // The labels sit on the map as on screen: a soft dark halo under light
      // ink, a light one under dark ink.
      context.shadowColor = style.textShadow === "none" ? "transparent" : haloFor(style.color);
      context.shadowBlur = style.textShadow === "none" ? 0 : Math.max(1, size / 5);
      context.fillText(words, textBox.x, textBox.y + textBox.h / 2, textBox.w + size);
      context.shadowBlur = 0;
      context.shadowColor = "transparent";
    }
  }
  context.restore();
}

/** The frame showing, as a PNG: captured as the map renders it, the legend
 * painted in, the caption and credits over it. */
export async function exportStill(host: GifHost, index: number, legend: HTMLElement | null): Promise<Blob> {
  const source = host.map.getCanvas();
  const canvas = document.createElement("canvas");
  canvas.width = source.width;
  canvas.height = source.height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("2D canvas unavailable for the PNG");
  await captureNextRender(host.map, context);
  if (legend) paintLegend(context, legend, host.map.getContainer().getBoundingClientRect());
  drawCaption(context, host.caption(index), host.credit());
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("PNG encoding failed"))), "image/png");
  });
}
