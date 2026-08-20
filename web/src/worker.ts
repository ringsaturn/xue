/// <reference lib="webworker" />
/**
 * Decode Worker: owns the WASM decoder.
 *
 * Two modes share the same decode/recycle protocol with the main thread:
 *
 * - `init` (full mode): the main thread downloaded the whole bundle and
 *   transfers it here; the compressed bytes live exactly once, inside WASM
 *   linear memory.
 * - `init-stream` (streaming mode): the worker receives only the bundle URL
 *   and fetches the structural prefix (header + metadata + index) itself via
 *   an HTTP range request, then range-fetches each temporal group's payload
 *   bytes on demand. Background prefetch is windowed:
 *   the main thread sends `prefetch-window` messages naming the forecast
 *   hours just ahead of the playhead plus a concurrency cap, and the worker
 *   keeps at most that many group fetches in flight for hours inside the
 *   window — beyond the window it stays idle. Fetches triggered by a decode
 *   always run first. `progress` messages report resident bytes, and a final
 *   `resident` message fires once every group is local. A non-206 response
 *   to the first range request aborts with a regular pre-ready `error`,
 *   which the main thread treats as fatal — it probes range support before
 *   choosing this mode.
 *
 * Decoded planes are returned as transferable ArrayBuffers; the main thread
 * may send buffers back for reuse through the `recycle` message.
 */

import wasmInit, { WasmBundle, WasmStreamingBundle } from "./wasm/xue";
import wasmUrl from "./wasm/xue_bg.wasm?url";

interface InitMessage {
  type: "init";
  buffer: ArrayBuffer;
}

interface InitStreamMessage {
  type: "init-stream";
  url: string;
  byteLength: number;
  /** Echoed back in progress/resident messages so the main thread can
   * attribute them to the right variable session. */
  variableKey: string;
}

interface DecodeMessage {
  type: "decode";
  requestId: number;
  generation: number;
  variableId: number;
  forecastHour: number;
}

interface RecycleMessage {
  type: "recycle";
  buffer: ArrayBuffer;
}

interface ClearMessage {
  type: "clear-cache";
}

interface PrefetchWindowMessage {
  type: "prefetch-window";
  /** Forecast hours to keep resident, in fetch-priority order. */
  hours: number[];
  /** Maximum concurrent background range fetches. */
  concurrency: number;
}

type WorkerRequest =
  | InitMessage
  | InitStreamMessage
  | DecodeMessage
  | RecycleMessage
  | ClearMessage
  | PrefetchWindowMessage;

const FIRST_FETCH_LENGTH = 16 * 1024;
const PREFETCH_RETRIES = 3;

let bundle: WasmBundle | null = null;
let streaming: WasmStreamingBundle | null = null;
let streamUrl = "";
let variableKey = "";
/** Spans currently being fetched for a decode, keyed by span start. */
const spanFetches = new Map<number, Promise<void>>();
let decodeFetchCount = 0;
const recycled: ArrayBuffer[] = [];

/** Windowed prefetch state (see module docs). */
let windowHours: number[] = [];
let windowConcurrency = 0;
let allHours: number[] = [];
/** Every variable in the bundle; the wind bundle carries two. */
let bundleNumericIds: number[] = [];
let residentAnnounced = false;
let prefetchFailures = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function post(message: unknown, transfer: Transferable[] = []): void {
  (self as unknown as DedicatedWorkerGlobalScope).postMessage(message, transfer);
}

/** Fetch [start, end) of the stream URL. Throws unless the server honors the
 * range exactly (206 with the requested byte count). */
async function fetchRange(start: number, end: number): Promise<Uint8Array> {
  const response = await fetch(streamUrl, { headers: { Range: `bytes=${start}-${end - 1}` } });
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

/** Reads dataOffset from the fixed header so the prefix fetch can be sized.
 * Full validation happens in WASM; this only needs the one field. */
function headerDataOffset(bytes: Uint8Array): number {
  const magic = "XUE\0\0\0\0\0";
  if (bytes.byteLength < 80) throw new Error("incomplete bundle header");
  for (let index = 0; index < magic.length; index += 1) {
    if (bytes[index] !== magic.charCodeAt(index)) throw new Error("not a Xue bundle");
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const offset = view.getBigUint64(56, true);
  if (offset > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("data offset exceeds safe integer range");
  return Number(offset);
}

function postProgress(): void {
  if (!streaming) return;
  post({
    type: "progress",
    variableKey,
    bytes: streaming.dataOffset() + streaming.residentPayloadBytes(),
    totalBytes: streaming.fileSize(),
  });
}

/** Fetch and insert one missing span, deduplicating concurrent requests. */
function ensureSpan(start: number, end: number, forDecode: boolean): Promise<void> {
  const running = spanFetches.get(start);
  if (running) return running;
  if (forDecode) decodeFetchCount += 1;
  const task = (async () => {
    try {
      const bytes = await fetchRange(start, end);
      streaming?.insertRange(start, bytes);
      postProgress();
      prefetchFailures = 0;
    } finally {
      spanFetches.delete(start);
      if (forDecode) decodeFetchCount -= 1;
      // A freed slot (or a completed decode fetch) may unblock the window.
      pumpPrefetch();
    }
  })();
  spanFetches.set(start, task);
  return task;
}

/** Fill the prefetch window: keep up to `windowConcurrency` background range
 * fetches in flight for the window's missing groups, pausing entirely while
 * decode-triggered fetches are active so interactive scrubbing always wins
 * the bandwidth. Idle once the window is resident; announces `resident` when
 * the whole bundle is. */
function pumpPrefetch(): void {
  const session = streaming;
  if (!session || windowConcurrency <= 0) return;
  if (decodeFetchCount > 0) return;
  if (prefetchFailures >= PREFETCH_RETRIES) return; // wait for the next window update
  let missing = false;
  for (const hour of windowHours) {
    for (const numericId of bundleNumericIds) {
      if (spanFetches.size >= windowConcurrency) return;
      const span = session.missingGroupSpan(numericId, hour);
      if (!span) continue;
      if (spanFetches.has(span[0]!)) continue;
      missing = true;
      void ensureSpan(span[0]!, span[1]!, false).catch(() => {
        prefetchFailures += 1;
        if (prefetchFailures < PREFETCH_RETRIES && !retryTimer) {
          retryTimer = setTimeout(() => {
            retryTimer = null;
            pumpPrefetch();
          }, 1000 * prefetchFailures);
        }
      });
    }
  }
  if (!missing && spanFetches.size === 0 && !residentAnnounced) {
    if (
      allHours.every((hour) => bundleNumericIds.every((numericId) => !session.missingGroupSpan(numericId, hour)))
    ) {
      residentAnnounced = true;
      post({ type: "resident", variableKey });
    }
  }
}

async function initStream(message: InitStreamMessage): Promise<void> {
  await wasmInit(wasmUrl);
  streamUrl = message.url;
  variableKey = message.variableKey;
  const started = performance.now();
  const first = await fetchRange(0, Math.min(FIRST_FETCH_LENGTH, message.byteLength));
  const dataOffset = headerDataOffset(first);
  if (dataOffset > message.byteLength) throw new Error("data offset exceeds bundle length");
  let prefix = first.subarray(0, Math.min(dataOffset, first.byteLength));
  if (dataOffset > first.byteLength) {
    const rest = await fetchRange(first.byteLength, dataOffset);
    const joined = new Uint8Array(dataOffset);
    joined.set(prefix, 0);
    joined.set(rest, prefix.byteLength);
    prefix = joined;
  }
  const session = new WasmStreamingBundle(prefix);
  if (session.fileSize() !== message.byteLength) {
    throw new Error("bundle-declared length does not match the manifest");
  }
  streaming = session;
  post({
    type: "ready",
    metadataJson: session.metadataJson(),
    planeLength: session.planeLength(),
    openMs: performance.now() - started,
  });
  postProgress();

  const metadata = JSON.parse(session.metadataJson()) as {
    time: { firstForecastHour: number; stepHours: number; frameCount: number };
    variables: { numericId: number }[];
  };
  bundleNumericIds = metadata.variables.map((variable) => variable.numericId);
  allHours = Array.from(
    { length: metadata.time.frameCount },
    (_, index) => metadata.time.firstForecastHour + index * metadata.time.stepHours,
  );
  residentAnnounced = false;
  // A prefetch-window message may have arrived before init finished.
  pumpPrefetch();
}

async function decodeStreaming(message: DecodeMessage): Promise<Uint8Array> {
  const session = streaming;
  if (!session) throw new Error("bundle is not initialized");
  const span = session.missingGroupSpan(message.variableId, message.forecastHour);
  if (span) await ensureSpan(span[0]!, span[1]!, true);
  return session.decodeFrame(message.variableId, message.forecastHour);
}

self.onmessage = async (event: MessageEvent<WorkerRequest>) => {
  const message = event.data;
  try {
    if (message.type === "init") {
      await wasmInit(wasmUrl);
      const bytes = new Uint8Array(message.buffer);
      const started = performance.now();
      bundle = new WasmBundle(bytes);
      // The transferred ArrayBuffer reference dies with this scope, leaving
      // the compressed bundle resident only in WASM linear memory.
      post({
        type: "ready",
        metadataJson: bundle.metadataJson(),
        planeLength: bundle.planeLength(),
        openMs: performance.now() - started,
      });
      return;
    }
    if (message.type === "init-stream") {
      await initStream(message);
      return;
    }
    if (message.type === "decode") {
      if (!bundle && !streaming) throw new Error("bundle is not initialized");
      const started = performance.now();
      const plane = bundle
        ? bundle.decodeFrame(message.variableId, message.forecastHour)
        : await decodeStreaming(message);
      let buffer = recycled.pop();
      if (!buffer || buffer.byteLength !== plane.byteLength) buffer = new ArrayBuffer(plane.byteLength);
      new Uint8Array(buffer).set(plane);
      post(
        {
          type: "frame",
          requestId: message.requestId,
          generation: message.generation,
          variableId: message.variableId,
          forecastHour: message.forecastHour,
          decodeMs: performance.now() - started,
          buffer,
        },
        [buffer],
      );
      return;
    }
    if (message.type === "recycle") {
      if (recycled.length < 4) recycled.push(message.buffer);
      return;
    }
    if (message.type === "prefetch-window") {
      windowHours = message.hours;
      windowConcurrency = message.concurrency;
      prefetchFailures = 0;
      pumpPrefetch();
      return;
    }
    if (message.type === "clear-cache") {
      bundle?.clearCache();
      streaming?.clearCache();
    }
  } catch (error) {
    post({
      type: "error",
      requestId: message.type === "decode" ? message.requestId : undefined,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};

post({ type: "booted" });
