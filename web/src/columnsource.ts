// The run's column store as a pinned point reads it: which bundles it serves,
// each as the same thing a session is to the probe panel (`PointSession`: the
// bundle's own metadata and axis, and a worker that answers a `series`
// request), and what becomes of a bundle it fails to serve. The store is read
// on a hub worker (`zarr/columns.ts`); one array's column holds every level
// of it, so the series of a whole profile are cut here from a few reads.
//
// Nothing depends on the store. A manifest without one, an origin without
// ranges, a group that does not open and a read that fails past the store's
// own retries all end the same way: the bundle is not served, and the panel
// opens its session as it does on any other run.
import { identifyBundle, identityForBundleId, sameChart, type VariableIdentity } from "./identity";
import {
  parseBundleMetadata,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  type ZarrStoreDescriptor,
} from "./manifest";
import type { DecodeChannel, PointSession } from "./session";
import type { DataBackend } from "./urlstate";
import { zarrProbeUrl, zarrRootUrl } from "./zarr/channel";
import type { ColumnReadMessage, ZarrSeriesMessage } from "./zarr/protocol";

/** Where one variable's series is in the store: the array, and the place of
 * its level in each frame of the array's column. */
interface ColumnMember {
  array: string;
  level: number;
  levels: number;
}

/** One bundle the store serves. */
export interface ColumnBundle {
  metadata: BundleMetadata;
  identity: VariableIdentity;
  /** The variables to read, in the order the bundle's chart takes them. */
  variables: BundleVariable[];
  /** Where each is, by `numericId`. */
  members: Map<number, ColumnMember>;
}

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
    members: { bundle: string; variable: Record<string, unknown> }[];
  }[];
}

/**
 * The bundles of a store's block, each with the metadata document it would
 * have embedded in a store of its own: the block's run and grid, its arrays'
 * axis, and its variables numbered in the order they appear.
 *
 * A bundle is left out, and so read through a session, where that document
 * does not read as one, where its arrays do not share an axis, or where what
 * its variables say is not the chart its name promises: half a wind pair
 * would otherwise be served as a scalar.
 */
export function columnBundles(blockJson: string): Map<ForecastBundleId, ColumnBundle> {
  const block = JSON.parse(blockJson) as ColumnBlock;
  const found = new Map<string, { time: string; variables: Record<string, unknown>[]; members: Map<number, ColumnMember> } | null>();
  for (const array of block.arrays) {
    const time = JSON.stringify(array.time ?? block.time);
    for (const [level, { bundle, variable }] of array.members.entries()) {
      if (!found.has(bundle)) found.set(bundle, { time, variables: [], members: new Map() });
      const entry = found.get(bundle);
      if (!entry) continue;
      if (entry.time !== time) {
        found.set(bundle, null);
        continue;
      }
      const numericId = entry.variables.length + 1;
      entry.variables.push({ ...variable, numericId });
      entry.members.set(numericId, { array: array.id, level, levels: array.members.length });
    }
  }
  const bundles = new Map<ForecastBundleId, ColumnBundle>();
  for (const [id, entry] of found) {
    if (!entry) continue;
    let metadata: BundleMetadata;
    try {
      metadata = parseBundleMetadata(
        JSON.stringify({
          schemaVersion: 3,
          model: block.model,
          product: block.product,
          runTime: block.runTime,
          profile: block.profile,
          time: JSON.parse(entry.time) as unknown,
          grid: block.grid,
          variables: entry.variables,
        }),
      );
    } catch {
      continue;
    }
    const derived = identifyBundle(metadata.variables);
    const named = identityForBundleId(id);
    if (!derived || !named || !sameChart(named, derived.identity)) continue;
    bundles.set(id, { metadata, identity: derived.identity, variables: derived.variables, members: entry.members });
  }
  return bundles;
}

/** A whole series out of a column: the codes of one level, frame by frame. */
export function levelSeries(codes: Uint8Array, member: ColumnMember): Uint8Array<ArrayBuffer> {
  const series = new Uint8Array(Math.floor(codes.length / member.levels));
  for (let frame = 0; frame < series.length; frame += 1) series[frame] = codes[frame * member.levels + member.level]!;
  return series;
}

/** What one bundle is read through at the pin: a layer on screen that holds
 * its whole series (`shown`), so that its row and its layer's reading are one
 * number; else the column store once it has answered for this pin
 * (`answered`); else the open session; else the store still to answer. A
 * session opened since, or one that has read nothing, does not take the place
 * of a series already in hand. */
export function readingFrom<S extends PointSession>(
  column: PointSession | undefined,
  session: S | undefined,
  shown: (session: S) => boolean,
  answered: (column: PointSession) => boolean,
): PointSession | undefined {
  if (!column || (session && shown(session))) return session;
  return answered(column) ? column : (session ?? column);
}

/** What one bundle's series is asked of: a session with a layer on screen,
 * so that its row and its layer's reading are one number; else the column
 * store; else a session open for something else. Neither: the bundle's
 * session is to be opened. */
export function askingOf<S extends PointSession>(
  column: PointSession | undefined,
  session: S | undefined,
  onScreen: (session: S) => boolean,
): { column: PointSession } | { session: S } | null {
  if (session && (!column || onScreen(session))) return { session };
  return column ? { column } : null;
}

/** A served series, in the shape a decode channel's `series` reply has. */
export interface ColumnSeriesReply {
  requestId: number;
  generation: number;
  variableId: number;
  column: number;
  row: number;
  buffer: ArrayBuffer;
}

/** What the source tells the panel. */
export interface ColumnListener {
  series: (session: PointSession, reply: ColumnSeriesReply) => void;
  /** A store's bundles are known now, or fewer are served than were. */
  changed: () => void;
}

export interface ColumnSourceDeps {
  readonly manifest: ForecastManifest | null;
  readonly manifestUrl: string | null;
  readonly dataBackend: DataBackend;
  /** A session key of the registry's, unique among every session's. */
  readonly nextKey: () => number;
  /** Whether the object's origin answers ranges. */
  readonly streams: (url: string) => Promise<boolean>;
  readonly openChannel: () => DecodeChannel;
}

/** How long a pin waits to learn what a store serves, its group read and its
 * origin's answer about ranges together. The manifest does not say which
 * bundles the store holds, so every bundle of the run that has no session
 * waits for it, those the store turns out not to hold too. Past it the run is
 * read as one without the store. */
const COLUMN_OPEN_MS = 8000;

/** A `series` request waiting on a column read. */
interface Waiter {
  session: PointSession;
  member: ColumnMember;
  request: ZarrSeriesMessage;
}

interface OpenStore {
  /** The bundles the store may be read for. */
  serves: (id: ForecastBundleId) => boolean;
  /** Its group has been read, or will not be. */
  settled: boolean;
  channel: DecodeChannel;
  /** The reads under way, by request id, and the id by what is read. */
  waiting: Map<number, { key: string; waiters: Waiter[] }>;
  asked: Map<string, number>;
}

interface OpenRun {
  stores: OpenStore[];
  sessions: Map<ForecastBundleId, PointSession>;
}

export function createColumnSource(deps: ColumnSourceDeps) {
  let current: OpenRun | null = null;
  let listener: ColumnListener = { series: () => {}, changed: () => {} };
  let nextRequestId = 1;

  /** Take a store's bundles away for the rest of the run, all of them or the
   * ones given, and have the panel open their sessions. */
  function withdraw(run: OpenRun, ids: Iterable<ForecastBundleId>, reason: string): void {
    const withdrawn = [...ids].filter((id) => run.sessions.delete(id));
    if (run !== current || withdrawn.length === 0) return;
    // Diagnostics stay English.
    console.warn(`column store: ${withdrawn.join(", ")} read from their own stores:`, reason);
    listener.changed();
  }

  function readSeries(store: OpenStore, waiter: Waiter): void {
    const { request, member } = waiter;
    const key = `${member.array}:${request.row}:${request.column}`;
    const asked = store.asked.get(key);
    if (asked !== undefined) {
      store.waiting.get(asked)!.waiters.push(waiter);
      return;
    }
    const requestId = nextRequestId++;
    store.asked.set(key, requestId);
    store.waiting.set(requestId, { key, waiters: [waiter] });
    store.channel.postMessage({
      type: "column",
      requestId,
      array: member.array,
      row: request.row,
      column: request.column,
    } satisfies ColumnReadMessage);
  }

  /** The waiters of an answered read, which is then no longer under way. */
  function answered(store: OpenStore, requestId: unknown): Waiter[] {
    const read = typeof requestId === "number" ? store.waiting.get(requestId) : undefined;
    if (!read) return [];
    store.waiting.delete(requestId as number);
    store.asked.delete(read.key);
    return read.waiters;
  }

  /** Open a store's channel; resolves with the group's block. */
  function openGroup(run: OpenRun, store: OpenStore, descriptor: ZarrStoreDescriptor, manifestUrl: string): Promise<string> {
    return new Promise((resolve, reject) => {
      const failed = (message: string): void => reject(new Error(message));
      store.channel.onerror = (event) => failed(event.message || "worker failed");
      store.channel.onmessage = (event: MessageEvent) => {
        if (run !== current) return;
        const message = event.data as Record<string, unknown>;
        if (message.type === "ready") resolve(String(message.blockJson));
        else if (message.type === "column") {
          const codes = new Uint8Array(message.buffer as ArrayBuffer);
          for (const { session, member, request } of answered(store, message.requestId)) {
            const { requestId, generation, variableId, column, row } = request;
            // The panel drops a series that is not its axis's, and would be
            // left without the level.
            if (codes.length !== session.metadata.time.frameCount * member.levels) {
              withdraw(run, [session.id], "column does not match the time axis");
              continue;
            }
            listener.series(session, { requestId, generation, variableId, column, row, buffer: levelSeries(codes, member).buffer });
          }
        } else if (message.type === "error") {
          const text = String(message.message ?? "failed");
          if (message.requestId === undefined) failed(text);
          else withdraw(run, new Set(answered(store, message.requestId).map((waiter) => waiter.session.id)), text);
        }
      };
      store.channel.postMessage({
        type: "init-columns",
        url: zarrRootUrl(descriptor.path, manifestUrl),
        crc32: descriptor.crc32,
      });
    });
  }

  /** Open one store and serve its bundles, those `serves` admits. */
  async function openStore(
    run: OpenRun,
    descriptor: ZarrStoreDescriptor,
    manifest: ForecastManifest,
    manifestUrl: string,
    serves: (id: ForecastBundleId) => boolean,
  ): Promise<void> {
    const channel = deps.openChannel();
    const store: OpenStore = { serves, settled: false, channel, waiting: new Map(), asked: new Map() };
    run.stores.push(store);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const quiet = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("no answer")), COLUMN_OPEN_MS);
    });
    try {
      const [blockJson, ranges] = await Promise.race([
        Promise.all([openGroup(run, store, descriptor, manifestUrl), deps.streams(zarrProbeUrl(descriptor, manifestUrl))]),
        quiet,
      ]);
      if (!ranges) throw new Error("range requests unsupported");
      if (run !== current) return;
      const ids: ForecastBundleId[] = [];
      for (const [id, bundle] of columnBundles(blockJson)) {
        if (!serves(id)) continue;
        if (Date.parse(bundle.metadata.runTime) !== Date.parse(manifest.runTime)) throw new Error("not this run's");
        const session: PointSession = {
          id,
          key: deps.nextKey(),
          metadata: bundle.metadata,
          identity: bundle.identity,
          vector: bundle.identity.vector,
          variable: bundle.variables[0]!,
          variables: bundle.variables,
          leadOffsets: null,
          worker: {
            postMessage(message: unknown) {
              const request = message as ZarrSeriesMessage;
              const member = bundle.members.get(request.variableId);
              if (member) readSeries(store, { session, member, request });
            },
          },
        };
        run.sessions.set(id, session);
        ids.push(id);
      }
      // A worker that dies from here on takes its store's bundles with it.
      channel.onerror = (event) => withdraw(run, ids, event.message || "worker failed");
    } catch (error) {
      channel.terminate();
      if (run !== current) return;
      console.warn("column store: not opened:", error instanceof Error ? error.message : error);
    } finally {
      clearTimeout(timer);
      store.settled = true;
    }
    if (run === current) listener.changed();
  }

  function begin(manifest: ForecastManifest, manifestUrl: string): void {
    const run: OpenRun = { stores: [], sessions: new Map() };
    current = run;
    if (deps.dataBackend !== "zarr") return;
    const borrowed = new Set(manifest.borrowed?.bundles ?? []);
    const lent = manifest.borrowed?.columns;
    if (manifest.columns) void openStore(run, manifest.columns, manifest, manifestUrl, (id) => !borrowed.has(id));
    if (lent) void openStore(run, lent, manifest, manifestUrl, (id) => borrowed.has(id));
  }

  return {
    listen(to: ColumnListener): void {
      listener = to;
    },
    /** Whether a store that may serve the bundle is still being opened, which
     * the first ask of a run starts; `changed` is called once it is not. */
    awaiting(id: ForecastBundleId): boolean {
      if (!current && deps.manifest && deps.manifestUrl) begin(deps.manifest, deps.manifestUrl);
      return current?.stores.some((store) => !store.settled && store.serves(id)) ?? false;
    },
    /** The bundle as the store serves it, or undefined when it does not. */
    session: (id: ForecastBundleId): PointSession | undefined => current?.sessions.get(id),
    /** The pin has moved: what was asked for the old one is let go. */
    supersede(): void {
      for (const store of current?.stores ?? []) {
        if (store.waiting.size === 0) continue;
        store.waiting.clear();
        store.asked.clear();
        store.channel.postMessage({ type: "supersede" });
      }
    },
    /** The run is over, or its store is another now. */
    end(): void {
      for (const store of current?.stores ?? []) store.channel.terminate();
      current = null;
    },
  };
}

export type ColumnSource = ReturnType<typeof createColumnSource>;
