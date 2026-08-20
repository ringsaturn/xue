/**
 * Browser-native WebCodecs decode path for the temperature video artifact.
 *
 * Ports the logic proven out in scripts/webcodecs_spike/spike.js into a
 * `DecodeChannel`
 * that mimics the Worker message protocol `worker.ts` speaks
 * (`booted` -> `init` -> `ready`, then `decode` -> `frame`/`error`), so
 * `main.ts` can hold either a real Worker (Xue/WASM path) or this
 * channel (video path) in the same `VariableSession.worker` field without
 * branching anywhere else: `requestDecode`, `handleDecodedFrame`, buffer
 * recycling, and session teardown are all unchanged.
 *
 * The channel accepts either the complete stream as an ArrayBuffer, or a URL
 * plus byte length for on-demand delivery: each seek range-fetches only the
 * GOP it needs (the per-frame index carries exact byte offsets), and
 * background prefetch is windowed: `prefetch-window`
 * messages name the forecast hours just ahead of the playhead plus a
 * concurrency cap, and the channel keeps at most that many GOP fetches in
 * flight inside the window — decode-triggered fetches always go first.
 * `progress`/`resident` messages mirror the streaming Worker protocol.
 *
 * Not WASM, not a Worker: `VideoDecoder` already runs off the main thread
 * internally, and decoding a GOP-6 stream is cheap enough (measured p95
 * 33.9ms for a full 6-frame decode) that a dedicated Worker isn't needed.
 */

import { parseBundleMetadata } from "./manifest";

export interface DecodeChannel {
  postMessage(message: unknown, transfer?: Transferable[]): void;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  terminate(): void;
}

export interface VideoFrameIndexEntry {
  offset: number;
  length: number;
  keyframe: boolean;
}

interface AvcDecoderConfig {
  format?: "annexb" | "avc";
}

type DecoderConfig = VideoDecoderConfig & { avc?: AvcDecoderConfig };

export async function isWebCodecsSupported(codec: string, width: number, height: number): Promise<boolean> {
  if (typeof VideoDecoder === "undefined") return false;
  const config: DecoderConfig = { codec, codedWidth: width, codedHeight: height, avc: { format: "annexb" } };
  try {
    const result = await VideoDecoder.isConfigSupported(config);
    return result.supported === true;
  } catch {
    return false;
  }
}

export function nearestKeyframe(frames: VideoFrameIndexEntry[], target: number): number {
  for (let index = target; index >= 0; index -= 1) {
    if (frames[index]?.keyframe) return index;
  }
  return 0;
}

/** The full GOP containing `target`: from its keyframe through the last frame
 * before the next keyframe. Reaching `target` requires decoding everything
 * from the keyframe anyway, and extending to the GOP tail costs only a few ms
 * while turning nearby scrubs and sequential playback into cache hits. */
export function gopRange(frames: VideoFrameIndexEntry[], target: number): { from: number; to: number } {
  const from = nearestKeyframe(frames, target);
  let to = target;
  for (let index = target + 1; index < frames.length; index += 1) {
    const entry = frames[index];
    if (!entry || entry.keyframe) break;
    to = index;
  }
  return { from, to };
}

/** Byte span `[start, end)` covering frames `from..to` of the stream. Valid
 * because the encoder writes frames back to back in presentation order. */
export function gopByteSpan(
  frames: VideoFrameIndexEntry[],
  from: number,
  to: number,
): { start: number; end: number } {
  const first = frames[from];
  const last = frames[to];
  if (!first || !last) throw new Error(`GOP range ${from}-${to} outside the index`);
  return { start: first.offset, end: last.offset + last.length };
}

/** Copies the luma plane out of a decoded VideoFrame, dropping row stride. */
async function extractPlane(frame: VideoFrame, width: number, height: number): Promise<Uint8Array> {
  const rect = { x: 0, y: 0, width: frame.codedWidth, height: frame.codedHeight };
  const size = frame.allocationSize({ rect });
  const dest = new Uint8Array(size);
  const layout = await frame.copyTo(dest, { rect });
  const planeLayout = layout[0];
  if (!planeLayout) throw new Error("video frame has no plane layout");
  const packed = new Uint8Array(width * height);
  for (let row = 0; row < height; row += 1) {
    const rowStart = planeLayout.offset + row * planeLayout.stride;
    packed.set(dest.subarray(rowStart, rowStart + width), row * width);
  }
  return packed;
}

/** The complete stream up front, or a range-capable URL for on-demand GOPs. */
export type VideoStreamSource =
  | { kind: "buffer"; buffer: ArrayBuffer }
  | { kind: "url"; url: string; byteLength: number; variableKey: string };

export interface VideoAssetOptions {
  source: VideoStreamSource;
  frames: VideoFrameIndexEntry[];
  codec: string;
  width: number;
  height: number;
  metadataJson: string;
}

const PREFETCH_RETRIES = 3;

class VideoDecodeChannel implements DecodeChannel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;

  private readonly config: DecoderConfig;
  private readonly firstForecastHour: number;
  private readonly stepHours: number;
  /** Encoded bytes per frame; fully populated in buffer mode, filled by
   * range fetches in url mode. */
  private readonly chunks: (Uint8Array | undefined)[];
  private residentStreamBytes = 0;
  /** In-flight GOP fetches keyed by span start, for deduplication. */
  private readonly spanFetches = new Map<number, Promise<void>>();
  private decodeFetchCount = 0;
  private terminated = false;
  /** Windowed prefetch state: frame indices to keep resident, in order. */
  private windowIndices: number[] = [];
  private windowConcurrency = 0;
  private residentAnnounced = false;
  private prefetchFailures = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: VideoAssetOptions) {
    this.config = {
      codec: options.codec,
      codedWidth: options.width,
      codedHeight: options.height,
      avc: { format: "annexb" },
    };
    const metadata = parseBundleMetadata(options.metadataJson);
    this.firstForecastHour = metadata.time.firstForecastHour;
    this.stepHours = metadata.time.stepHours;
    this.chunks = new Array<Uint8Array | undefined>(options.frames.length);
    if (options.source.kind === "buffer") {
      const bytes = new Uint8Array(options.source.buffer);
      options.frames.forEach((entry, index) => {
        this.chunks[index] = bytes.subarray(entry.offset, entry.offset + entry.length);
      });
    }
    queueMicrotask(() => this.emit({ type: "booted" }));
  }

  private emit(data: unknown): void {
    if (this.terminated) return;
    this.onmessage?.({ data } as MessageEvent);
  }

  postMessage(message: unknown): void {
    const request = message as {
      type: string;
      requestId?: number;
      generation?: number;
      variableId?: number;
      forecastHour?: number;
      hours?: number[];
      concurrency?: number;
    };
    if (request.type === "init") {
      this.emit({
        type: "ready",
        metadataJson: this.options.metadataJson,
        planeLength: this.options.width * this.options.height,
        openMs: 0,
      });
      return;
    }
    if (request.type === "decode") {
      void this.decodeGop(request);
      return;
    }
    if (request.type === "prefetch-window") {
      this.windowIndices = (request.hours ?? []).map((hour) =>
        Math.round((hour - this.firstForecastHour) / this.stepHours),
      );
      this.windowConcurrency = request.concurrency ?? 0;
      this.prefetchFailures = 0;
      this.pumpPrefetch();
      return;
    }
    // "recycle" and "clear-cache": no-op, this path allocates a fresh buffer per decode.
  }

  /** Fetch [start, end) of the stream URL; requires an exact 206. */
  private async fetchRange(url: string, start: number, end: number): Promise<Uint8Array> {
    const response = await fetch(url, { headers: { Range: `bytes=${start}-${end - 1}` } });
    if (response.status !== 206) {
      await response.body?.cancel();
      throw new Error(`range requests unsupported (HTTP ${response.status})`);
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength !== end - start) {
      throw new Error(`range response length mismatch: expected ${end - start}, got ${bytes.byteLength}`);
    }
    return bytes;
  }

  /** Make frames from..to resident, deduplicating concurrent GOP fetches. */
  private ensureGop(from: number, to: number, forDecode: boolean): Promise<void> {
    const source = this.options.source;
    if (source.kind !== "url") return Promise.resolve();
    let missing = false;
    for (let index = from; index <= to; index += 1) {
      if (!this.chunks[index]) missing = true;
    }
    if (!missing) return Promise.resolve();
    const { start, end } = gopByteSpan(this.options.frames, from, to);
    const running = this.spanFetches.get(start);
    if (running) return running;
    if (forDecode) this.decodeFetchCount += 1;
    const task = (async () => {
      try {
        const bytes = await this.fetchRange(source.url, start, end);
        for (let index = from; index <= to; index += 1) {
          const entry = this.options.frames[index];
          if (!entry || this.chunks[index]) continue;
          this.chunks[index] = bytes.subarray(entry.offset - start, entry.offset - start + entry.length);
          this.residentStreamBytes += entry.length;
        }
        this.emit({
          type: "progress",
          variableKey: source.variableKey,
          bytes: this.residentStreamBytes,
          totalBytes: source.byteLength,
        });
        this.prefetchFailures = 0;
      } finally {
        this.spanFetches.delete(start);
        if (forDecode) this.decodeFetchCount -= 1;
        // A freed slot (or a completed decode fetch) may unblock the window.
        this.pumpPrefetch();
      }
    })();
    this.spanFetches.set(start, task);
    return task;
  }

  /** Fill the prefetch window (see class docs); decode fetches always win. */
  private pumpPrefetch(): void {
    const source = this.options.source;
    if (source.kind !== "url" || this.terminated || this.windowConcurrency <= 0) return;
    if (this.decodeFetchCount > 0) return;
    if (this.prefetchFailures >= PREFETCH_RETRIES) return; // wait for the next window update
    const { frames } = this.options;
    let missing = false;
    for (const index of this.windowIndices) {
      if (this.spanFetches.size >= this.windowConcurrency) return;
      if (index < 0 || index >= frames.length || this.chunks[index]) continue;
      missing = true;
      const { from, to } = gopRange(frames, index);
      void this.ensureGop(from, to, false).catch(() => {
        this.prefetchFailures += 1;
        if (this.prefetchFailures < PREFETCH_RETRIES && !this.retryTimer && !this.terminated) {
          this.retryTimer = setTimeout(() => {
            this.retryTimer = null;
            this.pumpPrefetch();
          }, 1000 * this.prefetchFailures);
        }
      });
    }
    if (!missing && this.spanFetches.size === 0 && !this.residentAnnounced) {
      if (this.chunks.every((chunk) => chunk !== undefined)) {
        this.residentAnnounced = true;
        this.emit({ type: "resident", variableKey: source.variableKey });
      }
    }
  }

  /** Decodes the whole GOP containing the requested frame and emits every
   * decoded plane as its own `frame` message, so the main-thread plane cache
   * keeps all of them. Only the requested target carries `requestId`; the
   * siblings are pure cache warm-up. */
  private async decodeGop(request: {
    requestId?: number;
    generation?: number;
    variableId?: number;
    forecastHour?: number;
  }): Promise<void> {
    const { frames, width, height } = this.options;
    const hour = request.forecastHour ?? 0;
    const target = Math.round((hour - this.firstForecastHour) / this.stepHours);
    if (target < 0 || target >= frames.length) {
      this.emit({ type: "error", requestId: request.requestId, message: `target frame ${target} outside the index` });
      return;
    }
    const { from, to } = gopRange(frames, target);
    const started = performance.now();
    try {
      await this.ensureGop(from, to, true);
    } catch (error) {
      this.emit({
        type: "error",
        requestId: request.requestId,
        message: error instanceof Error ? error.message : String(error),
      });
      return;
    }
    if (this.terminated) return;
    let failed = false;
    let targetEmitted = false;
    let chain: Promise<void> = Promise.resolve();

    const fail = (error: unknown): void => {
      if (failed) return;
      failed = true;
      try {
        decoder.close();
      } catch {
        // Already closed.
      }
      this.emit({
        type: "error",
        requestId: targetEmitted ? undefined : request.requestId,
        message: error instanceof Error ? error.message : String(error),
      });
    };

    const decoder = new VideoDecoder({
      output: (frame) => {
        const index = frame.timestamp;
        chain = chain.then(async () => {
          try {
            if (failed) return;
            const plane = await extractPlane(frame, width, height);
            if (index === target) targetEmitted = true;
            this.emit({
              type: "frame",
              requestId: index === target ? request.requestId : undefined,
              generation: request.generation,
              variableId: request.variableId,
              forecastHour: this.firstForecastHour + index * this.stepHours,
              decodeMs: performance.now() - started,
              buffer: plane.buffer,
            });
          } finally {
            frame.close();
          }
        });
      },
      error: (error) => fail(error),
    });
    decoder.configure(this.config);
    for (let index = from; index <= to; index += 1) {
      const entry = frames[index];
      const data = this.chunks[index];
      if (!entry || !data) continue;
      decoder.decode(
        new EncodedVideoChunk({
          type: entry.keyframe ? "key" : "delta",
          timestamp: index,
          data,
        }),
      );
    }
    decoder
      .flush()
      .then(() => chain)
      .then(() => {
        if (failed) return;
        decoder.close();
        if (!targetEmitted) fail(new Error(`frame ${target} was not emitted by the decoder`));
      })
      .catch(fail);
  }

  terminate(): void {
    // No persistent VideoDecoder to release: each decodeGop() call opens
    // and closes its own, since a GOP-6 decode is cheap (p95 33.9ms measured).
    // The flag stops the background prefetch loop and mutes late messages.
    this.terminated = true;
  }
}

export function createVideoDecodeChannel(options: VideoAssetOptions): DecodeChannel {
  return new VideoDecodeChannel(options);
}
