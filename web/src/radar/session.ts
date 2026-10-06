/** One radar site's sweeps, read for playback: which sweep stands at a
 * time, and the units that hold them, fetched near the playhead first.
 *
 * A unit is what one decode reads (`feed.ts`): a case's round, every sweep
 * of five minutes out of one range of a store's shard (`docs/nexrad.md`
 * §4), or a live sweep's own Level 3 object (§7). Units are asked for in
 * time order outward from the playhead — the one it stands on, then the
 * ones it is about to play, then behind it — never more than
 * `MAX_IN_FLIGHT` at once, and held under a byte budget that evicts whole
 * units farthest from the playhead first, the way the frame cache evicts
 * planes. A product is only read once it is shown: velocity costs nothing
 * until someone switches to it. */

import type { RadarSiteFeed, RadarUnit } from "./feed";
import type { RadarChunkReply, RadarChunkRequest, RadarDecodeRequest } from "./worker";
import { BEAMS, type RadarProduct, type RadarSite } from "./schema";

/** Units being fetched or decoded at once. */
const MAX_IN_FLIGHT = 4;
/** How far behind the playhead a sweep still stands for it: one missed
 * volume scan at the slowest clear-air cadence. Past this the site shows
 * nothing rather than an old picture. */
export const SWEEP_MAX_AGE_MS = 15 * 60 * 1000;
/** Units fetched ahead of the playhead before any behind it. */
const AHEAD_ROUNDS = 8;

export interface RadarSweep {
  time: number;
  codes: Uint8Array;
  gates: number;
}

interface Slot {
  unit: RadarUnit;
  sweep: number;
  time: number;
}

interface Resident {
  codes: Uint8Array;
  gates: number;
  /** Its unit's place on the clock, for eviction by distance. */
  time: number;
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

function decodeRemote(request: RadarDecodeRequest): Promise<RadarChunkReply> {
  const id = nextRequest++;
  const worker = pool()[id % pool().length]!;
  return new Promise((resolve) => {
    waiting.set(id, resolve);
    worker.postMessage({ ...request, id } as RadarChunkRequest);
  });
}

export class RadarSession {
  private readonly slots = new Map<RadarProduct, Slot[]>();
  private readonly resident = new Map<string, Resident>();
  private readonly pending = new Set<string>();
  private readonly failed = new Set<string>();
  private readonly started = performance.now();
  private readonly feed: RadarSiteFeed;
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
    readonly site: RadarSite,
    feed: (onChange: () => void) => RadarSiteFeed,
    product: RadarProduct,
    private readonly budgetBytes: number,
    private readonly onChange: () => void,
  ) {
    this.product = product;
    this.feed = feed(() => this.unitsChanged());
    this.feed.watch(product);
  }

  setProduct(product: RadarProduct): void {
    if (product === this.product) return;
    this.product = product;
    this.feed.watch(product);
    this.pump();
  }

  /** The playhead moved: what it stands on is fetched first. */
  setTime(time: number): void {
    this.time = time;
    this.pump();
  }

  /** The sweep standing at `time` for the shown product, when its unit is
   * in hand; null when the site has none that recent, or it is still on
   * its way (then it is asked for). */
  sweepAt(time: number): RadarSweep | null {
    const slot = this.slotAt(this.product, time);
    if (!slot) return null;
    const resident = this.resident.get(this.key(this.product, slot.unit));
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
   * from the feed alone: what the readout names even before the bytes
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
    this.feed.close();
    this.resident.clear();
    this.pending.clear();
  }

  /** A live feed's units changed: sweeps arrived or aged out. Residents of
   * units gone from the feed are dropped. */
  private unitsChanged(): void {
    if (this.closed) return;
    this.slots.clear();
    const live = new Set<string>();
    const products = new Set([...this.resident.keys()].map((key) => key.split(":")[0] as RadarProduct));
    for (const product of products) {
      for (const unit of this.feed.units(product)) live.add(this.key(product, unit));
    }
    for (const [key, resident] of this.resident) {
      if (live.has(key)) continue;
      this.stats.residentBytes -= resident.codes.byteLength;
      this.resident.delete(key);
    }
    this.pump();
    this.onChange();
  }

  private key(product: RadarProduct, unit: RadarUnit): string {
    return `${product}:${unit.key}`;
  }

  private timeline(product: RadarProduct): Slot[] {
    let slots = this.slots.get(product);
    if (slots) return slots;
    slots = [];
    for (const unit of this.feed.units(product)) {
      unit.times.forEach((time, sweep) => slots!.push({ unit, sweep, time }));
    }
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

  /** The units to hold, in the order to fetch them: the one the playhead
   * stands on, the ones ahead of it, then the ones behind. */
  private wanted(): RadarUnit[] {
    const units = [...this.feed.units(this.product)].sort((a, b) => a.time - b.time);
    if (units.length === 0) return [];
    const time = this.time ?? units[units.length - 1]!.times.at(-1)!;
    const here = this.slotAt(this.product, time)?.unit ?? units.find((unit) => unit.time >= time) ?? units[0]!;
    const position = Math.max(0, units.indexOf(here));
    const ahead = units.slice(position, position + 1 + AHEAD_ROUNDS);
    const behind = units.slice(0, position).reverse();
    const beyond = units.slice(position + 1 + AHEAD_ROUNDS);
    return [...ahead, ...behind, ...beyond];
  }

  private pump(): void {
    if (this.closed) return;
    for (const unit of this.wanted()) {
      if (this.pending.size >= MAX_IN_FLIGHT) break;
      const key = this.key(this.product, unit);
      if (this.resident.has(key) || this.pending.has(key) || this.failed.has(key)) continue;
      if (!this.fits(unit)) break;
      this.fetch(unit);
    }
  }

  /** Whether a unit can be held: room under the budget, made by evicting
   * units farther from the playhead than it is. */
  private fits(unit: RadarUnit): boolean {
    const product = this.product;
    const distance = (time: number) => Math.abs(time - (this.time ?? 0));
    while (this.stats.residentBytes + unit.bytes > this.budgetBytes) {
      let victim: string | null = null;
      let farthest = distance(unit.time);
      for (const [key, resident] of this.resident) {
        const away = distance(resident.time) + (key.startsWith(`${product}:`) ? 0 : Number.MAX_SAFE_INTEGER / 2);
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

  private fetch(unit: RadarUnit): void {
    const product = this.product;
    const key = this.key(product, unit);
    this.pending.add(key);
    void decodeRemote(unit.request).then((reply) => {
      this.pending.delete(key);
      if (this.closed) return;
      if (!reply.ok) {
        this.failed.add(key);
        console.warn(`radar: ${this.site.id} ${product} ${unit.key} not read: ${reply.error}`);
      } else {
        const codes = new Uint8Array(reply.codes);
        this.resident.set(key, { codes, gates: unit.request.gates, time: unit.time, touched: ++this.touch });
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
