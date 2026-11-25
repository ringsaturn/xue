// The run's column store as a vertical section reads it: opened on a channel
// of its own, so that a pinned point moving does not let a section's read go
// and a section does not fill the chunks a pin holds, and read array by array
// over all the cells of a plan.
//
// Nothing of it is kept here: the codes go to the renderer as they arrive,
// and a line drawn anew is read anew.
import { gridMapFor, parseGrid } from "../gridmap";
import { columnQuantity } from "../identity";
import {
  axisUnitSeconds,
  frameOffsets,
  parseBundleMetadata,
  type BundleMetadata,
  type ForecastManifest,
  type ZarrStoreDescriptor,
} from "../manifest";
import type { DecodeChannel } from "../session";
import type { DataBackend } from "../urlstate";
import { zarrProbeUrl, zarrRootUrl } from "../zarr/channel";
import type { ColumnCellsMessage, ColumnCellsReply, ColumnInitMessage } from "../zarr/protocol";
import type { SectionArray, SectionPlan, SectionStore, SectionUnavailable } from "./types";

export interface SectionSourceDeps {
  readonly manifest: ForecastManifest | null;
  readonly manifestUrl: string | null;
  readonly dataBackend: DataBackend;
  /** Whether the object's origin answers ranges. */
  readonly streams: (url: string) => Promise<boolean>;
  readonly openChannel: () => DecodeChannel;
}

/** There is no section to draw, and why. */
export class SectionError extends Error {
  constructor(
    readonly reason: SectionUnavailable,
    message: string,
  ) {
    super(message);
    this.name = "SectionError";
  }
}

export interface SectionRead {
  /** Resolves with every cell handed over. Rejects with the failure, or with
   * an `AbortError` for a read that was cancelled, superseded by another or
   * ended with its run. */
  done: Promise<void>;
  cancel(): void;
}

/** `codes` are those of the plan's cells `indices` names: cell by cell, each
 * frame by frame, the code of every level. Both are the receiver's to keep or
 * to transfer. */
export type SectionCells = (array: string, indices: Uint32Array, codes: Uint8Array<ArrayBuffer>) => void;

/** The parts of the `xue_columns` block read here, which the worker has held
 * to its schema and to the array documents. */
interface ColumnBlock {
  model: string;
  product: string;
  runTime: string;
  profile: string;
  time: unknown;
  grid: unknown;
  arrays: {
    id: string;
    time?: unknown;
    levels?: number[];
    members: { variable: Record<string, unknown> }[];
  }[];
}

/**
 * The store a block describes. An array's members are read as the variables
 * of a bundle on the array's axis would be; one that does not read so is left
 * out. `blockShape` is `[height, width]` cells.
 */
export function sectionStore(blockJson: string, blockShape: readonly number[]): SectionStore {
  const block = JSON.parse(blockJson) as ColumnBlock;
  const grid = parseGrid(block.grid);
  const arrays = new Map<string, SectionArray>();
  for (const array of block.arrays) {
    let metadata: BundleMetadata;
    try {
      metadata = parseBundleMetadata(
        JSON.stringify({
          schemaVersion: 3,
          model: block.model,
          product: block.product,
          runTime: block.runTime,
          profile: block.profile,
          time: array.time ?? block.time,
          grid: block.grid,
          variables: array.members.map(({ variable }, level) => ({ ...variable, numericId: level + 1 })),
        }),
      );
    } catch (error) {
      console.warn(`column store: ${array.id} left out of the section:`, error instanceof Error ? error.message : error);
      continue;
    }
    const unit = axisUnitSeconds(metadata.time);
    const quantity = columnQuantity(metadata.variables[0]!);
    arrays.set(array.id, {
      id: array.id,
      // Half of one quantity and half of another is none.
      quantity: metadata.variables.every((variable) => columnQuantity(variable) === quantity) ? quantity : null,
      levels: array.levels ?? null,
      offsets: frameOffsets(metadata.time).map((offset) => offset * unit),
      members: metadata.variables,
    });
  }
  return {
    model: block.model,
    runTime: Date.parse(block.runTime),
    grid: gridMapFor(grid),
    width: grid.width,
    height: grid.height,
    blockHeight: blockShape[0]!,
    blockWidth: blockShape[1]!,
    arrays,
  };
}

/** How long a store may take to open. The store's own retries end within it
 * where the network answers at all. */
const OPEN_MS = 20_000;

/** How long a read may go without a word. The store's own retries of a request
 * end within it; a worker that has died silently answers nothing. */
const STALL_MS = 45_000;

interface ReadUnderWay {
  onCells: SectionCells;
  /** The arrays not whole yet, by request id. */
  requests: Map<number, SectionArray>;
  stall: ReturnType<typeof setTimeout> | undefined;
  resolve: () => void;
  reject: (error: Error) => void;
}

interface OpenRun {
  /** Which store it is, under which crc. */
  key: string;
  channel: DecodeChannel;
  opening: Promise<SectionStore>;
  store: SectionStore | null;
  read: ReadUnderWay | null;
  /** Fails an open that is still under way. */
  fail: (error: Error) => void;
}

function aborted(message: string): Error {
  return new DOMException(message, "AbortError");
}

export function createSectionSource(deps: SectionSourceDeps) {
  let current: OpenRun | null = null;
  let nextRequestId = 1;
  /** The models that have had a store on this page: a run of one without it
   * is a run whose store is not there yet. */
  const shipped = new Set<string>();

  /** Take the read off the run and tell its worker to go no further. */
  function stopRead(run: OpenRun, error: Error): void {
    const { read } = run;
    if (!read) return;
    run.read = null;
    clearTimeout(read.stall);
    run.channel.postMessage({ type: "supersede" });
    read.reject(error);
  }

  function close(run: OpenRun, error: Error): void {
    if (run === current) current = null;
    const { read } = run;
    run.read = null;
    clearTimeout(read?.stall);
    run.channel.terminate();
    read?.reject(error);
    run.fail(error);
  }

  /** The worker has been asked, or has answered: its next word is waited for
   * from here. A read that gets none ends with its
   * channel, so that the next open has a new one. */
  function heard(run: OpenRun, read: ReadUnderWay): void {
    clearTimeout(read.stall);
    read.stall = setTimeout(() => {
      if (run.read === read) close(run, new SectionError("failed", "the column store stopped answering"));
    }, STALL_MS);
  }

  function received(run: OpenRun, message: ColumnCellsReply): void {
    const { read } = run;
    const array = read?.requests.get(message.requestId);
    if (!read || !array) return;
    heard(run, read);
    const length = message.indices.length * array.offsets.length * array.members.length;
    if (message.buffer.byteLength !== length) {
      stopRead(run, new Error(`${array.id} does not match its axis and levels`));
      return;
    }
    try {
      if (length > 0) read.onCells(array.id, message.indices, new Uint8Array(message.buffer));
    } catch (error) {
      stopRead(run, error instanceof Error ? error : new Error(String(error)));
      return;
    }
    // What was handed over may have been answered with another read.
    if (!message.done || run.read !== read) return;
    read.requests.delete(message.requestId);
    if (read.requests.size > 0) return;
    run.read = null;
    clearTimeout(read.stall);
    read.resolve();
  }

  /** Open a store's channel and read its group. */
  function begin(descriptor: ZarrStoreDescriptor, manifest: ForecastManifest, manifestUrl: string, key: string): OpenRun {
    const channel = deps.openChannel();
    let described!: (store: SectionStore) => void;
    const ready = new Promise<SectionStore>((resolve) => {
      described = resolve;
    });
    let fail!: (error: Error) => void;
    const stopped = new Promise<never>((_, reject) => {
      fail = reject;
    });
    stopped.catch(() => {});
    const run: OpenRun = { key, channel, opening: ready, store: null, read: null, fail };
    // A worker that dies takes the store with it, and the next open has a new
    // one.
    channel.onerror = (event) => close(run, new SectionError("failed", event.message || "worker failed"));
    channel.onmessage = (event: MessageEvent) => {
      if (run !== current) return;
      const message = event.data as Record<string, unknown>;
      if (message.type === "ready") {
        try {
          described(sectionStore(String(message.blockJson), message.blockShape as number[]));
        } catch (error) {
          fail(new SectionError("failed", error instanceof Error ? error.message : String(error)));
        }
      } else if (message.type === "cells") received(run, event.data as ColumnCellsReply);
      else if (message.type === "error") {
        const text = String(message.message ?? "failed");
        if (message.requestId === undefined) fail(new SectionError("failed", text));
        else if (run.read?.requests.has(message.requestId as number)) stopRead(run, new Error(text));
      }
    };
    const timer = setTimeout(() => fail(new SectionError("failed", "the column store did not open")), OPEN_MS);
    run.opening = (async () => {
      try {
        const ranges = deps.streams(zarrProbeUrl(descriptor, manifestUrl)).then((answered) => {
          if (!answered) throw new SectionError("origin", "range requests unsupported");
        });
        const [store] = await Promise.race([Promise.all([ready, ranges]), stopped]);
        if (run !== current) throw aborted("the run ended");
        if (store.runTime !== Date.parse(manifest.runTime)) throw new SectionError("failed", "the column store is another run's");
        run.store = store;
        return store;
      } catch (error) {
        const failure = error instanceof Error ? error : new Error(String(error));
        if (run === current) close(run, failure);
        throw failure instanceof SectionError ? failure : new SectionError("failed", failure.message);
      } finally {
        clearTimeout(timer);
      }
    })();
    // Whoever asked has the rejection; one that ended with the run may have
    // nobody left to hear it.
    run.opening.catch(() => {});
    channel.postMessage({
      type: "init-columns",
      url: zarrRootUrl(descriptor.path, manifestUrl),
      crc32: descriptor.crc32,
      heldBytes: 0,
    } satisfies ColumnInitMessage);
    return run;
  }

  /** Which store the run on the page names, or null. */
  function keyOf(): string | null {
    const { manifest, manifestUrl } = deps;
    const descriptor = manifest?.columns ?? manifest?.borrowed?.columns;
    return descriptor && manifestUrl ? `${zarrRootUrl(descriptor.path, manifestUrl)}?v=${descriptor.crc32}` : null;
  }

  return {
    /**
     * The run's column store, or the lender's whole where the run borrows its
     * upper air. Rejects with a `SectionError`; asked again after that, it is
     * tried again.
     */
    async open(): Promise<SectionStore> {
      const { manifest, manifestUrl } = deps;
      if (!manifest || !manifestUrl) throw new SectionError("model", "no run is open");
      const descriptor = manifest.columns ?? manifest.borrowed?.columns;
      const lends = manifest.columns === undefined && manifest.borrowed !== undefined;
      if (!descriptor) {
        throw lends || shipped.has(manifest.model)
          ? new SectionError("run", "the run has no column store")
          : new SectionError("model", "the dataset has no column store");
      }
      if (deps.dataBackend !== "zarr") throw new SectionError("model", "the stores are not read on this backend");
      shipped.add(manifest.model);
      const key = keyOf()!;
      if (current && current.key !== key) close(current, aborted("another store"));
      current ??= begin(descriptor, manifest, manifestUrl, key);
      return current.opening;
    },

    /**
     * Read arrays of the open store over a plan's cells, in the order given,
     * `onCells` being handed each block's as it lands. An id the store does
     * not hold is passed over: `SectionStore.arrays` says which it does. A
     * read supersedes the one under way.
     */
    read(plan: SectionPlan, arrayIds: readonly string[], onCells: SectionCells): SectionRead {
      const run = current;
      const store = run?.store;
      if (!run || !store) {
        const done = Promise.reject(new SectionError("failed", "the column store is not open"));
        done.catch(() => {});
        return { done, cancel: () => {} };
      }
      stopRead(run, aborted("superseded"));
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const done = new Promise<void>((accept, refuse) => {
        resolve = accept;
        reject = refuse;
      });
      // A cancelled read has often nobody waiting on it.
      done.catch(() => {});
      const read: ReadUnderWay = { onCells, requests: new Map(), stall: undefined, resolve, reject };
      for (const id of new Set(arrayIds)) {
        const array = store.arrays.get(id);
        if (array && plan.cells.length > 0) read.requests.set(nextRequestId++, array);
      }
      if (read.requests.size === 0) {
        resolve();
        return { done, cancel: () => {} };
      }
      run.read = read;
      heard(run, read);
      for (const [requestId, array] of read.requests) {
        run.channel.postMessage({ type: "cells", requestId, array: array.id, cells: plan.cells } satisfies ColumnCellsMessage);
      }
      return {
        done,
        cancel: () => {
          if (run.read === read) stopRead(run, aborted("cancelled"));
        },
      };
    },

    /** Whether the store open is the one the run on the page names: a run
     * published again beside an unchanged store is still that store's. */
    get current(): boolean {
      return current !== null && current.key === keyOf();
    },

    /** The run is over, or its store is another now. */
    end(): void {
      if (current) close(current, aborted("the run ended"));
    },
  };
}

export type SectionSource = ReturnType<typeof createSectionSource>;
