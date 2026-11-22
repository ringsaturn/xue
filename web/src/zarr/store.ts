/**
 * A fetch-backed, range-coalescing Zarr store.
 *
 * Every object of a store lives under one root URL and is immutable, so the
 * store appends the manifest descriptor's `?v=<crc32>` to every URL the way
 * a bundle's own `?v=` busts the edge cache, and reads shard bytes with HTTP
 * range requests: a shard index as a suffix range (`bytes=-N`, which needs
 * no object length) or a prefix, and inner chunks by offset.
 *
 * What makes the store more than `fetch` is coalescing. A viewport covers a
 * rectangle of tiles, and a tile row's inner chunks sit back to back in the
 * shard, so a decode that asks for twenty-four chunks one `getRange` at a
 * time would cost twenty-four round trips where the `.xue` reader costs one
 * per tile row. Range requests made within one microtask are gathered per
 * object, sorted by offset, merged wherever the gap between neighbours is
 * at most `gap` bytes (a little over-read is cheaper than a round trip),
 * fetched as one range each and split back to their callers. A caller that
 * issues its requests synchronously — a loop over the tiles it needs —
 * gets the merged form without knowing.
 *
 * An origin that answers no range request at all (a static host without
 * `Accept-Ranges`) is served in whole-object mode: every range of an object
 * is cut from one GET of that object, kept for the store's life. A shard is
 * one temporal group of one variable whole, so a global view then costs
 * what the `.xue` path's whole download costs, group by group rather than
 * all at once — the last resort `main.ts` takes when nothing streams.
 */

export interface ByteRange {
  offset: number;
  length: number;
}

/** The last `suffixLength` bytes of an object, wherever it ends. */
interface SuffixRange {
  suffixLength: number;
}

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

/** Bytes between two ranges up to which they are fetched as one request.
 * 64 KB is a few inner chunks on a production grid: the gap a missing tile
 * leaves in an otherwise contiguous tile row. */
const COALESCE_GAP = 64 * 1024;

interface StoreStats {
  /** HTTP requests actually issued. */
  requests: number;
  /** Ranges callers asked for, before coalescing. */
  ranges: number;
  /** Bytes received, coalesced over-reads included. */
  bytes: number;
}

/** One merged request: the run it covers and which of the caller's ranges
 * (by index into the input) it answers. */
interface CoalescedRange extends ByteRange {
  members: number[];
}

/** Merge ranges of one object whose gaps are at most `gap` bytes. Input
 * order is free; overlapping ranges merge too. A negative gap disables
 * merging altogether, which is how the measurement harness sees the
 * uncoalesced request count. */
export function coalesceRanges(ranges: readonly ByteRange[], gap = COALESCE_GAP): CoalescedRange[] {
  const order = ranges.map((_, index) => index).sort((a, b) => ranges[a]!.offset - ranges[b]!.offset);
  const runs: CoalescedRange[] = [];
  for (const index of order) {
    const range = ranges[index]!;
    const last = runs[runs.length - 1];
    if (last && gap >= 0 && range.offset - (last.offset + last.length) <= gap) {
      last.length = Math.max(last.offset + last.length, range.offset + range.length) - last.offset;
      last.members.push(index);
      continue;
    }
    runs.push({ offset: range.offset, length: range.length, members: [index] });
  }
  return runs;
}

interface PendingRange {
  range: ByteRange;
  resolve: (bytes: Uint8Array) => void;
  reject: (error: unknown) => void;
}

/** How long to wait before asking again for an object whose request died on
 * the wire, attempt by attempt; after the last the failure is the caller's.
 *
 * Every request of a page to one origin rides one HTTP/2 connection, so when
 * that connection is closed under it — an edge does this to a client it
 * takes for a flood, and a pin that opens forty bundles at once looks like
 * one — every request in flight fails together, the frame on screen among
 * them. The browser has a new connection by the time anything asks again,
 * and the objects are immutable, so asking again is always right. Only a
 * request that never got an answer is retried: a status is an answer. */
const RETRY_DELAYS_MS: readonly number[] = [250, 1000];

/** How long a request may show no sign of life — before its response
 * arrives, and from one piece of its body to the next — before it is given
 * up and asked again like one that died on the wire.
 *
 * A request that never settles is worse than one that fails: it keeps its
 * place under `MAX_IN_FLIGHT` for good, and sixteen of them leave a worker
 * that can read nothing again — on a hub, every row of a pinned point at
 * once. Nothing promises that a request riding a connection the far end
 * closed is ever answered or failed, so the wait is bounded here. It is the
 * stall that is timed and not the transfer: a large object on a slow line
 * takes as long as it takes while its bytes keep arriving. */
const STALL_MS = 10_000;

/** The most requests one worker has on the wire at once, whichever of its
 * stores they belong to. A layer's worker rarely has a dozen; a hub that
 * hosts a pin's sessions would otherwise send a few hundred in one tick,
 * which is the flood. Requests past the limit wait their turn in order. */
const MAX_IN_FLIGHT = 16;
let inFlightHere = 0;
const waiting: (() => void)[] = [];

function takeSlot(): Promise<void> {
  if (inFlightHere < MAX_IN_FLIGHT) {
    inFlightHere += 1;
    return Promise.resolve();
  }
  // The slot is handed over by whoever frees one, so the count stays put.
  return new Promise((resolve) => waiting.push(resolve));
}

function freeSlot(): void {
  const next = waiting.shift();
  if (next) next();
  else inFlightHere -= 1;
}

/** What an object's origin answered: its status and, when that was a
 * success, the bytes. */
interface Answer {
  status: number;
  ok: boolean;
  bytes: Uint8Array | null;
}

interface ZarrStoreOptions {
  /** `fetch` to use; the tests and the measurement harness serve a
   * directory through one of their own. */
  fetch?: FetchLike;
  gap?: number;
  /** False when the origin serves no ranges: every `getRange` is then cut
   * from one whole GET of its object (see the module notes). True by
   * default — the main thread probes range support before opening. */
  ranges?: boolean;
  /** The waits before a request that died on the wire is asked again
   * (`RETRY_DELAYS_MS` by default); the tests pass zeros. */
  retryDelays?: readonly number[];
  /** How long a request may go quiet before it is given up (`STALL_MS` by
   * default); the tests pass milliseconds. */
  stallMs?: number;
}

/** A response's body, piece by piece, saying so at each piece (`alive`) so
 * that a transfer is timed by its stalls and not by its size. */
async function readBody(response: Response, alive: () => void, stalled: Promise<never>): Promise<Uint8Array> {
  const reader = response.body?.getReader();
  if (!reader) return new Uint8Array(await Promise.race([response.arrayBuffer(), stalled]));
  const pieces: Uint8Array[] = [];
  let length = 0;
  for (;;) {
    const { done, value } = await Promise.race([reader.read(), stalled]);
    if (done) break;
    pieces.push(value);
    length += value.byteLength;
    alive();
  }
  // One piece that is the whole of its own buffer is the body as it is.
  const only = pieces.length === 1 ? pieces[0]! : null;
  if (only && only.byteOffset === 0 && only.byteLength === only.buffer.byteLength) return only;
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const piece of pieces) {
    bytes.set(piece, offset);
    offset += piece.byteLength;
  }
  return bytes;
}

export class ZarrStore {
  readonly stats: StoreStats = { requests: 0, ranges: 0, bytes: 0 };
  /** Requests currently in flight. */
  inFlight = 0;

  private readonly root: string;
  private readonly fetchImpl: FetchLike;
  private readonly gap: number;
  private readonly ranges: boolean;
  private readonly retryDelays: readonly number[];
  private readonly stallMs: number;
  private readonly pending = new Map<string, PendingRange[]>();
  private flushScheduled = false;
  /** Whole objects in flight or held, in whole-object mode only. */
  private readonly whole = new Map<string, Promise<Uint8Array>>();

  constructor(
    root: string,
    private readonly crc32: string,
    options: ZarrStoreOptions = {},
  ) {
    this.root = root.replace(/\/+$/, "");
    this.fetchImpl = options.fetch ?? ((url, init) => fetch(url, init));
    this.gap = options.gap ?? COALESCE_GAP;
    this.ranges = options.ranges ?? true;
    this.retryDelays = options.retryDelays ?? RETRY_DELAYS_MS;
    this.stallMs = options.stallMs ?? STALL_MS;
  }

  /** Whether the store reads by range or by whole object. */
  get streams(): boolean {
    return this.ranges;
  }

  /** The URL of one object, with the store's version. */
  url(path: string): string {
    return `${this.root}/${path}?v=${this.crc32}`;
  }

  /** A whole object — a `zarr.json`. */
  async get(path: string): Promise<Uint8Array> {
    const answer = await this.request(path, undefined, (response) => response.ok);
    if (answer.bytes === null) throw new Error(`store object ${path} failed (HTTP ${answer.status})`);
    this.stats.bytes += answer.bytes.byteLength;
    return answer.bytes;
  }

  /** Part of an object. Byte ranges are batched per microtask and merged
   * (see the module notes); a suffix range goes out at once, since nothing
   * else can be merged with a span whose start is unknown. */
  getRange(path: string, range: ByteRange | SuffixRange): Promise<Uint8Array> {
    this.stats.ranges += 1;
    if (!this.ranges) return this.sliceWhole(path, range);
    if ("suffixLength" in range) return this.fetchSuffix(path, range.suffixLength);
    return new Promise((resolve, reject) => {
      const queue = this.pending.get(path) ?? [];
      queue.push({ range, resolve, reject });
      this.pending.set(path, queue);
      if (!this.flushScheduled) {
        this.flushScheduled = true;
        queueMicrotask(() => this.flush());
      }
    });
  }

  private flush(): void {
    this.flushScheduled = false;
    const batches = [...this.pending.entries()];
    this.pending.clear();
    for (const [path, queue] of batches) {
      for (const run of coalesceRanges(
        queue.map((entry) => entry.range),
        this.gap,
      )) {
        void this.fetchRun(path, run, queue);
      }
    }
  }

  private async fetchRun(path: string, run: CoalescedRange, queue: PendingRange[]): Promise<void> {
    try {
      const bytes = await this.fetchBytes(path, `bytes=${run.offset}-${run.offset + run.length - 1}`, run.length, true);
      for (const index of run.members) {
        const { range, resolve } = queue[index]!;
        const start = range.offset - run.offset;
        resolve(bytes.subarray(start, start + range.length));
      }
    } catch (error) {
      for (const index of run.members) queue[index]!.reject(error);
    }
  }

  /** Whole-object mode: one GET per object, every range a view of it. A
   * failed GET is forgotten so a retry can fetch again. */
  private async sliceWhole(path: string, range: ByteRange | SuffixRange): Promise<Uint8Array> {
    let object = this.whole.get(path);
    if (!object) {
      object = this.get(path);
      this.whole.set(path, object);
      object.catch(() => this.whole.delete(path));
    }
    const bytes = await object;
    if ("suffixLength" in range) return bytes.subarray(Math.max(0, bytes.byteLength - range.suffixLength));
    if (range.offset + range.length > bytes.byteLength) {
      throw new Error(`range beyond object: ${range.offset}+${range.length} of ${bytes.byteLength}`);
    }
    return bytes.subarray(range.offset, range.offset + range.length);
  }

  private fetchSuffix(path: string, suffixLength: number): Promise<Uint8Array> {
    return this.fetchBytes(path, `bytes=-${suffixLength}`, suffixLength, false);
  }

  /** One range request. A 206 is required — the main thread probes range
   * support before opening this channel, so anything else is an error, not
   * a fallback — and the byte count must be exact, except that a suffix
   * longer than the object is legitimately answered with the whole object. */
  private async fetchBytes(path: string, range: string, expected: number, exact: boolean): Promise<Uint8Array> {
    const answer = await this.request(path, range, (response) => response.status === 206);
    if (answer.bytes === null) throw new Error(`range requests unsupported (HTTP ${answer.status})`);
    const { bytes } = answer;
    this.stats.bytes += bytes.byteLength;
    if (exact ? bytes.byteLength !== expected : bytes.byteLength > expected || bytes.byteLength === 0) {
      throw new Error(`range response length mismatch: expected ${expected}, got ${bytes.byteLength}`);
    }
    return bytes;
  }

  /**
   * One request and its body, under the worker's limit and with a request
   * that died on the wire, or went quiet on it, asked again
   * (`RETRY_DELAYS_MS`, `STALL_MS`). `wanted` says which answers carry the
   * bytes the caller came for; any other is returned as its status with the
   * body let go, and is never retried.
   */
  private async request(
    path: string,
    range: string | undefined,
    wanted: (response: Response) => boolean,
  ): Promise<Answer> {
    this.stats.requests += 1;
    this.inFlight += 1;
    await takeSlot();
    try {
      for (let attempt = 0; ; attempt += 1) {
        try {
          return await this.attempt(path, range, wanted);
        } catch (error) {
          const delay = this.retryDelays[attempt];
          if (delay === undefined) throw error;
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }
    } finally {
      freeSlot();
      this.inFlight -= 1;
    }
  }

  /** One try at a request: its answer, or a rejection when the wire fails
   * or goes quiet for `stallMs`. */
  private async attempt(
    path: string,
    range: string | undefined,
    wanted: (response: Response) => boolean,
  ): Promise<Answer> {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let giveUp: (error: Error) => void = () => {};
    // Raced against every wait below. An aborted fetch is supposed to
    // reject, but the point is not to depend on a request settling at all.
    const stalled = new Promise<never>((_, reject) => {
      giveUp = reject;
    });
    stalled.catch(() => {});
    const alive = (): void => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        controller.abort();
        giveUp(new Error(`no answer for ${path} in ${this.stallMs / 1000} s`));
      }, this.stallMs);
    };
    alive();
    try {
      const init: RequestInit = { signal: controller.signal };
      if (range) init.headers = { Range: range };
      const response = await Promise.race([this.fetchImpl(this.url(path), init), stalled]);
      if (!wanted(response)) {
        await Promise.race([response.body?.cancel(), stalled]);
        return { status: response.status, ok: response.ok, bytes: null };
      }
      alive();
      return { status: response.status, ok: response.ok, bytes: await readBody(response, alive, stalled) };
    } finally {
      clearTimeout(timer);
    }
  }
}
