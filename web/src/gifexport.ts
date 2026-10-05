import type { Map as MaplibreMap } from "maplibre-gl";

import type { GifWorkerRequest, GifWorkerResponse } from "./gif.worker";

/**
 * The loop on screen saved as an animated GIF: the frames from the one
 * showing onward, each captured off the map's canvas as it is drawn, with
 * the variable, the valid time and the credits burnt in (a GIF travels
 * without the page around it, and the credits are owed wherever the picture
 * goes). The legend is not drawn: the colours are the palette's, and the
 * code names the field.
 */

/** Frames in one loop at most: two days of hourly frames. */
export const GIF_MAX_FRAMES = 48;
/** Wider than this the file grows past what a chat will take, for nothing a
 * phone can show. */
export const GIF_MAX_WIDTH = 720;
/** How long one frame may take to arrive before the export gives up. */
const FRAME_TIMEOUT_MS = 30_000;

export interface GifCaption {
  /** The variable's name, `Precipitation Rate`. */
  title: string;
  /** The model and code line, `GFS / PRATE SFC`. */
  code: string;
  /** The frame's stamp: lead and valid time. */
  stamp: string;
}

export interface GifHost {
  map: MaplibreMap;
  /** Show the frame if it is decoded, and ask for it otherwise. */
  show(index: number): boolean;
  caption(index: number): GifCaption;
  /** The credit line the data and the basemap ask for. */
  credit(): string;
  /** How long the frame is held in playback, in milliseconds. */
  holdMs(index: number): number;
}

export interface GifProgress {
  phase: "capture" | "encode";
  done: number;
  total: number;
}

/** The frames a loop covers: from the frame on screen onward, and back from
 * it when the axis ends first, so a loop opened near the end still runs
 * `max` frames long. */
export function gifFrameWindow(count: number, current: number, max = GIF_MAX_FRAMES): number[] {
  const length = Math.min(count, max);
  const start = Math.max(0, Math.min(current, count - length));
  return Array.from({ length }, (_, offset) => start + offset);
}

/** `xue-gfs-prate-sfc-20261005t1200z.gif`: the code line and the first
 * frame's valid time, in characters every file system takes. */
export function gifFileName(code: string, validTime: number): string {
  const slug = code.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const stamp = new Date(validTime).toISOString().slice(0, 16).replace(/[-:]/g, "").toLowerCase();
  return `xue-${slug || "map"}-${stamp}z.gif`;
}

/** The captured size: the canvas's own pixels, scaled down to the cap. */
export function gifSize(width: number, height: number, maxWidth = GIF_MAX_WIDTH): { width: number; height: number } {
  const scale = Math.min(1, maxWidth / width);
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function frameShown(host: GifHost, index: number, signal: AbortSignal): Promise<void> {
  const deadline = performance.now() + FRAME_TIMEOUT_MS;
  while (!host.show(index)) {
    if (signal.aborted) throw new DOMException("GIF export cancelled", "AbortError");
    if (performance.now() > deadline) throw new Error(`frame ${index} did not decode within ${FRAME_TIMEOUT_MS} ms`);
    await sleep(50);
  }
}

/** The basemap's tiles for this view, so the first frame is not drawn over
 * a half-loaded map. */
async function mapSettled(map: MaplibreMap): Promise<void> {
  if (map.areTilesLoaded()) return;
  await Promise.race([new Promise<void>((resolve) => map.once("idle", () => resolve())), sleep(10_000)]);
}

/** Draw the frame the map is about to render into `context`: the drawing
 * buffer is only readable in the task that drew it, which is the map's
 * `render` event, so the copy happens there. */
function captureNextRender(map: MaplibreMap, context: CanvasRenderingContext2D): Promise<void> {
  return new Promise((resolve) => {
    map.once("render", () => {
      context.drawImage(map.getCanvas(), 0, 0, context.canvas.width, context.canvas.height);
      resolve();
    });
    map.triggerRepaint();
  });
}

function drawCaption(context: CanvasRenderingContext2D, caption: GifCaption, credit: string): void {
  const { width, height } = context.canvas;
  // Sized off the short side, so a phone's tall frame reads like a wide one.
  const unit = Math.max(11, Math.round(Math.min(width, height) / 48));
  const pad = Math.round(unit * 0.9);
  const mono = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim() || "monospace";
  context.save();
  context.textBaseline = "top";
  // White on a dark shadow reads over a pale basemap and a dark one alike.
  context.fillStyle = "#fff";
  context.shadowColor = "rgba(0, 0, 0, 0.75)";
  context.shadowBlur = Math.round(unit / 3);
  context.font = `600 ${Math.round(unit * 1.5)}px system-ui, sans-serif`;
  context.fillText(caption.title, pad, pad);
  context.font = `500 ${unit}px ${mono}`;
  context.fillText(caption.code, pad, pad + Math.round(unit * 1.9));
  // The stamp and the credits sit on one strip along the bottom edge.
  const strip = Math.round(unit * 2.2);
  context.shadowBlur = 0;
  context.fillStyle = "rgba(0, 0, 0, 0.55)";
  context.fillRect(0, height - strip, width, strip);
  context.fillStyle = "#fff";
  context.textBaseline = "middle";
  context.font = `500 ${unit}px ${mono}`;
  context.fillText(caption.stamp, pad, height - strip / 2);
  const room = width - pad * 3 - context.measureText(caption.stamp).width;
  context.textAlign = "right";
  context.font = `500 ${Math.round(unit * 0.72)}px ${mono}`;
  let line = credit;
  while (line.length > 8 && context.measureText(line).width > room) line = `${line.slice(0, -2)}…`;
  context.fillText(line, width - pad, height - strip / 2);
  context.restore();
}

/** Capture the frames and encode them; resolves to the file. The map keeps
 * whatever frame was last captured, and the caller puts its own back. */
export async function exportGif(
  host: GifHost,
  frames: number[],
  onProgress: (progress: GifProgress) => void,
  signal: AbortSignal,
): Promise<Blob> {
  const { map } = host;
  const source = map.getCanvas();
  const size = gifSize(source.width, source.height);
  const canvas = document.createElement("canvas");
  canvas.width = size.width;
  canvas.height = size.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("2D canvas unavailable for the GIF");
  await mapSettled(map);
  const credit = host.credit();
  const captured: ArrayBuffer[] = [];
  const delays: number[] = [];
  for (const [position, index] of frames.entries()) {
    await frameShown(host, index, signal);
    await captureNextRender(map, context);
    drawCaption(context, host.caption(index), credit);
    captured.push(context.getImageData(0, 0, size.width, size.height).data.buffer as ArrayBuffer);
    delays.push(host.holdMs(index));
    onProgress({ phase: "capture", done: position + 1, total: frames.length });
  }
  if (signal.aborted) throw new DOMException("GIF export cancelled", "AbortError");
  const worker = new Worker(new URL("./gif.worker.ts", import.meta.url), { type: "module" });
  try {
    const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("GIF export cancelled", "AbortError")), { once: true });
      worker.onerror = (event) => reject(new Error(event.message || "GIF worker failed"));
      worker.onmessage = (event: MessageEvent<GifWorkerResponse>) => {
        const message = event.data;
        if (message.type === "progress") onProgress({ phase: "encode", done: message.done, total: message.total });
        else resolve(message.bytes);
      };
      const request: GifWorkerRequest = { width: size.width, height: size.height, frames: captured, delays };
      worker.postMessage(request, captured);
    });
    return new Blob([bytes], { type: "image/gif" });
  } finally {
    worker.terminate();
  }
}

/** Hand the file over: the share sheet where a phone has one for files,
 * a download everywhere else. */
export async function deliverGif(blob: Blob, name: string): Promise<void> {
  const file = new File([blob], name, { type: "image/gif" });
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  if (coarse && navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      return;
    } catch (error) {
      // Dismissing the sheet is not a failure; anything else falls through
      // to the download.
      if (error instanceof DOMException && error.name === "AbortError") return;
    }
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
