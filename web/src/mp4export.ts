import { ArrayBufferTarget, Muxer } from "mp4-muxer";

import {
  captureFrame,
  deliverFile,
  exportCancelled,
  exportFileName,
  type GifHost,
  type GifProgress,
  prepareCapture,
  sleep,
} from "./gifexport";

/**
 * The loop on screen saved as an H.264 MP4, encoded in the browser
 * (WebCodecs) and muxed in memory. It shares the GIF's capture loop, host
 * and captions, and differs in what it samples: at a steady 30 fps it takes
 * the pictures playback draws *between* data frames too, the shader blend
 * swept toward the next frame, so a data frame lasts in the file exactly as
 * long as it holds on screen at the speed chosen, and the motion reads as
 * continuous rather than stepped.
 */

/** Frames of data in one file at most: a frame per hour for ten days. */
export const MP4_MAX_FRAMES = 240;
/** The output's width cap: a 1080p-class frame is what players and chats
 * take without a second transcode. */
export const MP4_MAX_WIDTH = 1920;
/** The file's frame rate; the data frames are sampled onto this clock. */
export const MP4_FPS = 30;
/** Encodes allowed in flight before capture waits for the encoder. */
const ENCODE_QUEUE_LIMIT = 4;

/** H.264 profiles in order of preference: High, Main, Constrained Baseline,
 * each at level 4.0 (1080p30), and each asked for with hardware first. */
export const MP4_CODECS = ["avc1.640028", "avc1.4d0028", "avc1.42e028"] as const;
export const MP4_ACCELERATION = ["prefer-hardware", "no-preference"] as const satisfies readonly HardwareAcceleration[];

/** One sampled picture: which of the window's frames (by position) and how
 * far toward the next one, in [0, 1). */
export interface Mp4Step {
  frame: number;
  weight: number;
}

/** The output size: the canvas's own pixels, scaled down to the cap, and
 * even on both sides, which H.264's 4:2:0 subsampling needs. */
export function mp4Size(width: number, height: number, maxWidth = MP4_MAX_WIDTH): { width: number; height: number } {
  const scale = Math.min(1, maxWidth / width);
  const even = (value: number) => Math.max(2, Math.round((value * scale) / 2) * 2);
  return { width: even(width), height: even(height) };
}

/** The target bitrate: 9 Mbit/s for 1080p30, scaled a little under
 * linearly with the pixel count (a bigger picture needs fewer bits per
 * pixel) and with the frame rate (consecutive frames resemble each other
 * more), between what still looks clean and what a chat will take. */
export function mp4Bitrate(width: number, height: number, fps: number): number {
  const pixels = (width * height) / (1920 * 1080);
  const bitrate = 9e6 * pixels ** 0.9 * (fps / 30) ** 0.6;
  return Math.round(Math.min(45e6, Math.max(1.5e6, bitrate)));
}

/** The pictures to take, on the file's clock: each data frame covers the
 * span it holds on screen, and every tick that falls within the span
 * samples it at the fraction of the way through (the weight the shader blend
 * is swept to). The last frame is never blended, as playback does not blend
 * across the loop seam. */
export function mp4Schedule(holdsMs: readonly number[], fps = MP4_FPS): Mp4Step[] {
  if (holdsMs.length === 0) return [];
  const tick = 1000 / fps;
  const total = holdsMs.reduce((sum, hold) => sum + hold, 0);
  const count = Math.max(1, Math.round(total / tick));
  const steps: Mp4Step[] = [];
  let frame = 0;
  let start = 0;
  for (let position = 0; position < count; position += 1) {
    const time = position * tick;
    while (frame + 1 < holdsMs.length && time >= start + (holdsMs[frame] ?? 0)) {
      start += holdsMs[frame] ?? 0;
      frame += 1;
    }
    const hold = holdsMs[frame] ?? 0;
    const last = frame === holdsMs.length - 1;
    const weight = last || hold <= 0 ? 0 : Math.min(1, Math.max(0, (time - start) / hold));
    steps.push({ frame, weight });
  }
  return steps;
}

/** The encoder configurations to try, best first. */
export function mp4CodecCandidates(width: number, height: number, fps = MP4_FPS): VideoEncoderConfig[] {
  const bitrate = mp4Bitrate(width, height, fps);
  const candidates: VideoEncoderConfig[] = [];
  for (const codec of MP4_CODECS) {
    for (const hardwareAcceleration of MP4_ACCELERATION) {
      candidates.push({
        codec,
        width,
        height,
        bitrate,
        framerate: fps,
        hardwareAcceleration,
        latencyMode: "quality",
        bitrateMode: "variable",
        // Length-prefixed NAL units with the parameter sets in the chunk
        // metadata: what an MP4 sample table carries.
        avc: { format: "avc" },
      });
    }
  }
  return candidates;
}

/** The first configuration the browser's encoder takes, or null. */
export async function mp4EncoderConfig(width: number, height: number, fps = MP4_FPS): Promise<VideoEncoderConfig | null> {
  if (typeof VideoEncoder === "undefined" || typeof VideoFrame === "undefined") return null;
  for (const candidate of mp4CodecCandidates(width, height, fps)) {
    try {
      const result = await VideoEncoder.isConfigSupported(candidate);
      if (result.supported) return result.config ?? candidate;
    } catch {
      // An option the browser does not know is a rejection of that candidate.
    }
  }
  return null;
}

let supported: Promise<boolean> | null = null;

/** Whether this browser can encode the MP4 at all, probed once at 720p: no
 * WebCodecs, or no H.264 encoder behind it, and the control stays hidden. */
export function isMp4ExportSupported(): Promise<boolean> {
  supported ??= mp4EncoderConfig(1280, 720).then((config) => config !== null);
  return supported;
}

export function mp4FileName(code: string, validTime: number): string {
  return exportFileName(code, validTime, "mp4");
}

/** Capture the loop and encode it; resolves to the file. The map keeps
 * whatever picture was last captured, and the caller puts its own back. */
export async function exportMp4(
  host: GifHost,
  frames: number[],
  onProgress: (progress: GifProgress) => void,
  signal: AbortSignal,
): Promise<Blob> {
  const source = host.map.getCanvas();
  const size = mp4Size(source.width, source.height);
  const config = await mp4EncoderConfig(size.width, size.height, MP4_FPS);
  if (!config) throw new Error("no H.264 encoder configuration is supported");
  const target = await prepareCapture(host, size, "MP4");
  const schedule = mp4Schedule(frames.map((index) => host.holdMs(index)), MP4_FPS);
  const muxer = new Muxer({
    target: new ArrayBufferTarget(),
    video: { codec: "avc", width: size.width, height: size.height, frameRate: MP4_FPS },
    fastStart: "in-memory",
    firstTimestampBehavior: "offset",
  });
  // Boxed: the encoder reports from its own task, after the narrowing here.
  const failure: { error: Error | null } = { error: null };
  const encoder = new VideoEncoder({
    output: (chunk, meta) => muxer.addVideoChunk(chunk, meta),
    error: (error) => {
      failure.error = error;
    },
  });
  encoder.configure(config);
  const tickUs = Math.round(1e6 / MP4_FPS);
  try {
    for (const [position, step] of schedule.entries()) {
      if (signal.aborted) throw exportCancelled("MP4");
      if (failure.error) throw failure.error;
      await captureFrame(host, frames[step.frame]!, target, signal, step.weight);
      const frame = new VideoFrame(target.canvas, { timestamp: position * tickUs, duration: tickUs });
      // One key frame: a periodic I-frame re-seeds the particle trails and
      // the blend, and reads as a pulse in an otherwise smooth loop.
      encoder.encode(frame, { keyFrame: position === 0 });
      frame.close();
      while (encoder.encodeQueueSize > ENCODE_QUEUE_LIMIT) await sleep(5);
      if (position % 2 === 1 || position === schedule.length - 1) {
        onProgress({ phase: "capture", done: position + 1, total: schedule.length });
        await sleep(0);
      }
    }
    if (signal.aborted) throw exportCancelled("MP4");
    onProgress({ phase: "encode", done: 0, total: 1 });
    await encoder.flush();
    if (failure.error) throw failure.error;
    muxer.finalize();
    onProgress({ phase: "encode", done: 1, total: 1 });
    return new Blob([muxer.target.buffer], { type: "video/mp4" });
  } finally {
    if (encoder.state !== "closed") encoder.close();
  }
}

export function deliverMp4(blob: Blob, name: string): Promise<void> {
  return deliverFile(blob, name, "video/mp4");
}
