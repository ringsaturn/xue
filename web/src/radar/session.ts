/** One radar site's window, read for playback: which sweep stands at a
 * time, and the rounds that hold them, fetched near the playhead first.
 *
 * A round is the unit read: one range out of one store's shard gives the
 * site's every sweep of those five minutes (`docs/nexrad.md` §4), decoded in
 * a worker. Rounds are asked for in time order outward from the playhead —
 * the one it stands on, then the ones it is about to play, then behind it —
 * never more than `MAX_IN_FLIGHT` at once, and held under a byte budget that
 * evicts whole rounds farthest from the playhead first, the way the frame
 * cache evicts planes. A product is only read once it is shown: velocity
 * costs nothing until someone switches to it. */

import type { RadarChunkReply, RadarChunkRequest } from "./worker";
import { BEAMS, GATES, type RadarProduct, type RadarWindow } from "./schema";

/** Rounds being fetched or decoded at once. */
const MAX_IN_FLIGHT = 4;
/** How far behind the playhead a sweep still stands for it: one missed
 * volume scan at the slowest clear-air cadence. Past this the site shows
 * nothing rather than an old picture. */
export const SWEEP_MAX_AGE_MS = 15 * 60 * 1000;
/** Rounds fetched ahead of the playhead before any behind it. */
const AHEAD_ROUNDS = 8;

export interface RadarSweep {
  time: number;
  codes: Uint8Array;
  gates: number;
}

interface Slot {
  round: number;
  chunk: number;
  sweep: number;
  time: number;
}

interface Resident {
  codes: Uint8Array;
  gates: number;
  scans: number;
  touched: number;
}

export interface RadarSessionStats {
  residentBytes: number;
  peakResidentBytes: number;
  fetchedBytes: number;
  rounds: number;
  inFlight: number;
  firstSweepMs: number | null;
}

let workers: Worker[] = [];
let nextRequest = 1;
const waiting = new Map<number, (reply: RadarChunkReply) => void>();

function pool(): Worker[] {
  if (workers.length === 0) {
    const count = Math.max(1, Math.min(2, (navigator.hardwareConcurrency || 2) - 1));
    workers = Array.from({ length: count }, () => {
      const worker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
      worker.onmessage = (event: MessageEvent<RadarChunkReply>) => {
        const resolve = waiting.get(event.data.id);
        waiting.delete(event.data.id);
        resolve?.(event.data);
      };
      return worker;
    });
  }
  return workers;
}

function decodeRemote(request: Omit<RadarChunkRequest, "id">): Promise<RadarChunkReply> {
  const id = nextRequest++;
  const worker = pool()[id % pool().length]!;
  return new Promise((resolve) => {
    waiting.set(id, resolve);
    worker.postMessage({ ...request, id } satisfies RadarChunkRequest);
  });
}

export class RadarSession {
  private readonly slots = new Map<RadarProduct, Slot[]>();
  private readonly resident = new Map<string, Resident>();
  private readonly pending = new Set<string>();
  private readonly failed = new Set<string>();
  private readonly started = performance.now();
  private time: number | null = null;
  private product: RadarProduct;
  private touch = 0;
  private stats: RadarSessionStats = {
    residentBytes: 0,
    peakResidentBytes: 0,
    fetchedBytes: 0,
    rounds: 0,
    inFlight: 0,
    firstSweepMs: null,
  };
  private closed = false;

  constructor(
    private readonly window: RadarWindow,
    private readonly windowUrl: string,
    readonly siteIndex: number,
    product: RadarProduct,
    private readonly budgetBytes: number,
    private readonly onChange: () => void,
  ) {
    this.product = product;
  }

  get site() {
    return this.window.sites[this.siteIndex]!;
  }

  setProduct(product: RadarProduct): void {
    if (product === this.product) return;
    this.product = product;
    this.pump();
  }

  /** The playhead moved: what it stands on is fetched first. */
  setTime(time: number): void {
    this.time = time;
    this.pump();
  }

  /** The sweep standing at `time` for the shown product, when its round is
   * in hand; null when the site has none that recent, or it is still on
   * its way (then it is asked for). */
  sweepAt(time: number): RadarSweep | null {
    const slot = this.slotAt(this.product, time);
    if (!slot) return null;
    const resident = this.resident.get(this.key(this.product, slot.round));
    if (!resident) return null;
    resident.touched = ++this.touch;
    const plane = BEAMS * resident.gates;
    return {
      time: slot.time,
      gates: resident.gates,
      codes: resident.codes.subarray(slot.sweep * plane, (slot.sweep + 1) * plane),
    };
  }

  /** The newest sweep time of the shown product at or before `time`, read
   * from the manifest alone: what the readout names even before the bytes
   * arrive. */
  sweepTimeAt(time: number): number | null {
    return this.slotAt(this.product, time)?.time ?? null;
  }

  /** Every sweep time of the shown product, for a timeline's ticks. */
  sweepTimes(): number[] {
    return this.timeline(this.product).map((slot) => slot.time);
  }

  statsSnapshot(): RadarSessionStats {
    return { ...this.stats, inFlight: this.pending.size };
  }

  close(): void {
    this.closed = true;
    this.resident.clear();
    this.pending.clear();
  }

  private key(product: RadarProduct, round: number): string {
    return `${product}:${round}`;
  }

  private timeline(product: RadarProduct): Slot[] {
    let slots = this.slots.get(product);
    if (slots) return slots;
    slots = [];
    this.window.rounds.forEach((round, roundIndex) => {
      const block = round.products[product];
      block?.chunks.forEach((chunk, chunkIndex) => {
        if (chunk.site !== this.siteIndex) return;
        chunk.times.forEach((time, sweep) => slots!.push({ round: roundIndex, chunk: chunkIndex, sweep, time }));
      });
    });
    slots.sort((a, b) => a.time - b.time);
    this.slots.set(product, slots);
    return slots;
  }

  private slotAt(product: RadarProduct, time: number): Slot | null {
    const slots = this.timeline(product);
    let low = 0;
    let high = slots.length - 1;
    let found: Slot | null = null;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (slots[middle]!.time <= time) {
        found = slots[middle]!;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    return found && time - found.time <= SWEEP_MAX_AGE_MS ? found : null;
  }

  /** The rounds to hold, in the order to fetch them: the one the playhead
   * stands on, the ones ahead of it, then the ones behind. */
  private wanted(): number[] {
    const slots = this.timeline(this.product);
    if (slots.length === 0) return [];
    const rounds = [...new Set(slots.map((slot) => slot.round))].sort((a, b) => a - b);
    const time = this.time ?? slots[slots.length - 1]!.time;
    const here = this.slotAt(this.product, time)?.round ?? rounds.find((round) => this.window.rounds[round]!.time >= time) ?? rounds[0]!;
    const position = Math.max(0, rounds.indexOf(here));
    const ahead = rounds.slice(position, position + 1 + AHEAD_ROUNDS);
    const behind = rounds.slice(0, position).reverse();
    const beyond = rounds.slice(position + 1 + AHEAD_ROUNDS);
    return [...ahead, ...behind, ...beyond];
  }

  private pump(): void {
    if (this.closed) return;
    for (const round of this.wanted()) {
      if (this.pending.size >= MAX_IN_FLIGHT) break;
      const key = this.key(this.product, round);
      if (this.resident.has(key) || this.pending.has(key) || this.failed.has(key)) continue;
      if (!this.fits(round)) break;
      this.fetch(round);
    }
  }

  /** Whether a round can be held: room under the budget, made by evicting
   * rounds farther from the playhead than it is. */
  private fits(round: number): boolean {
    const product = this.product;
    const block = this.window.rounds[round]!.products[product]!;
    const chunk = block.chunks.find((item) => item.site === this.siteIndex)!;
    const need = chunk.sweeps * BEAMS * GATES[product];
    const distance = (index: number) => Math.abs(this.window.rounds[index]!.time - (this.time ?? 0));
    while (this.stats.residentBytes + need > this.budgetBytes) {
      let victim: string | null = null;
      let farthest = distance(round);
      for (const key of this.resident.keys()) {
        const index = Number(key.split(":")[1]);
        const away = distance(index) + (key.startsWith(`${product}:`) ? 0 : Number.MAX_SAFE_INTEGER / 2);
        if (away > farthest) {
          farthest = away;
          victim = key;
        }
      }
      if (victim === null) return false;
      this.stats.residentBytes -= this.resident.get(victim)!.codes.byteLength;
      this.resident.delete(victim);
    }
    return true;
  }

  private fetch(round: number): void {
    const product = this.product;
    const key = this.key(product, round);
    const entry = this.window.rounds[round]!;
    const block = entry.products[product]!;
    const chunk = block.chunks.find((item) => item.site === this.siteIndex)!;
    const roundUrl = new URL(entry.path, this.windowUrl);
    const shard = new URL(`${product}.zarr/${product}/c/0/0/0/0`, roundUrl);
    shard.searchParams.set("v", block.shardCrc32);
    this.pending.add(key);
    void decodeRemote({
      url: shard.href,
      offset: chunk.offset,
      length: chunk.length,
      scans: block.scans,
      sweeps: chunk.sweeps,
      gates: GATES[product],
    }).then((reply) => {
      this.pending.delete(key);
      if (this.closed) return;
      if (!reply.ok) {
        this.failed.add(key);
        console.warn(`radar: ${this.site.id} ${product} round ${round} not read: ${reply.error}`);
      } else {
        const codes = new Uint8Array(reply.codes);
        this.resident.set(key, { codes, gates: GATES[product], scans: chunk.sweeps, touched: ++this.touch });
        this.stats.residentBytes += codes.byteLength;
        this.stats.peakResidentBytes = Math.max(this.stats.peakResidentBytes, this.stats.residentBytes);
        this.stats.fetchedBytes += reply.bytes;
        this.stats.rounds += 1;
        if (this.stats.firstSweepMs === null) this.stats.firstSweepMs = performance.now() - this.started;
        this.onChange();
      }
      this.pump();
    });
  }
}
