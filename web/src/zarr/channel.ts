/**
 * Choosing the Zarr channel.
 *
 * The channel is the default: it is taken whenever the bundle — or the
 * resolution tier picked for it — carries a `zarr` descriptor in the
 * manifest, unless the session asked for the container (`?backend=xue`,
 * read in urlstate.ts). Streamed where the store's origin honours range
 * requests; where it does not, `main.ts` first tries the `.xue` container
 * and comes back to the store in whole-object mode only when the entry
 * ships no container at all. A run without stores, or a link with the
 * parameter, never touches this file's worker.
 *
 * A layer's session gets a worker of its own (`spawnZarrWorker`); a pinned
 * point's sessions share a few (`openZarrHubChannel`).
 */

import type { DataBackend } from "../urlstate";
import { zarrSegments, type VariableBundleDescriptor, type VariantDescriptor, type ZarrDescriptor } from "../manifest";
import type { DecodeChannel } from "../session";
import type {
  ColumnWorkerRequest,
  ZarrHubEndpoint,
  ZarrHubReply,
  ZarrHubRequest,
  ZarrInitStreamMessage,
  ZarrWorkerRequest,
} from "./protocol";

/** The store a session would open: the tier's own when one was picked,
 * else the bundle's, and none when the container was asked for — except
 * on an entry that ships nothing else, where the store is the only way to
 * open it whatever the parameter says. */
export function zarrStoreFor(
  backend: DataBackend,
  descriptor: VariableBundleDescriptor,
  variant: VariantDescriptor | null,
): ZarrDescriptor | undefined {
  const entry = variant ?? descriptor;
  if (backend !== "zarr" && entry.path !== undefined) return undefined;
  return entry.zarr;
}

/** The store's root URL, resolved against the manifest the way every
 * artifact path is; the manifest URL's own `?v=` does not carry over. */
export function zarrRootUrl(path: string, manifestUrl: string): string {
  return new URL(path, manifestUrl).href.replace(/\/+$/, "");
}

/** The URL of one object in the store, as the worker will request it —
 * what the main thread probes for range support before opening. */
export function zarrObjectUrl(root: string, path: string, crc32: string): string {
  return `${root}/${path}?v=${crc32}`;
}

/** The URL of the store's first root document, as the worker will request
 * it: what the main thread probes for range support before opening. A
 * segmented store's segments share an origin, so the first speaks for all. */
export function zarrProbeUrl(store: ZarrDescriptor, manifestUrl: string): string {
  const first = zarrSegments(store)[0]!;
  return zarrObjectUrl(zarrRootUrl(first.path, manifestUrl), "zarr.json", first.crc32);
}

/** Open `bundle` out of a store, or out of a window's segments, with every
 * root resolved against the manifest. The bundle id travels with the store
 * because a shared store's path names every bundle of its tier. */
export function zarrInitMessage(
  store: ZarrDescriptor,
  bundle: string,
  manifestUrl: string,
  variableKey: string,
  ranges = true,
  payloadBudgetBytes?: number,
): ZarrInitStreamMessage {
  const roots = zarrSegments(store).map((segment) => ({
    url: zarrRootUrl(segment.path, manifestUrl),
    crc32: segment.crc32,
  }));
  return {
    type: "init-stream",
    kind: "zarr",
    url: roots[0]!.url,
    crc32: roots[0]!.crc32,
    bundle,
    byteLength: store.byteLength,
    variableKey,
    ...("segments" in store ? { segments: roots } : {}),
    ...(ranges ? {} : { ranges }),
    ...(payloadBudgetBytes === undefined ? {} : { payloadBudgetBytes }),
  };
}

export function spawnZarrWorker(): Worker {
  return new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
}

/** The most hub workers a page keeps. A pin's sessions spend their time
 * waiting on range reads, so a few threads carry dozens of them; more than
 * a few is the crowd of workers the hubs exist to avoid. Half the cores,
 * between one and four. */
function hubLimit(): number {
  const cores = typeof navigator === "undefined" ? 4 : navigator.hardwareConcurrency || 4;
  return Math.max(1, Math.min(4, Math.floor(cores / 2)));
}

interface Hub {
  worker: Worker;
  channels: Map<number, HubChannel>;
}

const hubs: Hub[] = [];
let nextHubChannel = 1;

/** One session's channel on a hub: the `DecodeChannel` the session holds,
 * which wraps what it posts in the hub's envelope and is handed what the
 * hub unwraps for it. */
class HubChannel implements DecodeChannel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  private closed = false;

  constructor(
    private readonly hub: Hub,
    private readonly id: number,
    endpoint: ZarrHubEndpoint,
  ) {
    hub.channels.set(id, this);
    const open: ZarrHubRequest = { kind: "open", channel: id };
    if (endpoint !== "bundle") open.endpoint = endpoint;
    hub.worker.postMessage(open);
  }

  postMessage(message: unknown, transfer: Transferable[] = []): void {
    if (this.closed) return;
    this.hub.worker.postMessage(
      { kind: "message", channel: this.id, message: message as ZarrWorkerRequest | ColumnWorkerRequest } satisfies ZarrHubRequest,
      transfer,
    );
  }

  terminate(): void {
    if (this.closed) return;
    this.closed = true;
    this.hub.channels.delete(this.id);
    this.hub.worker.postMessage({ kind: "close", channel: this.id } satisfies ZarrHubRequest);
  }
}

function spawnHub(): Hub {
  const worker = new Worker(new URL("./hub.worker.ts", import.meta.url), { type: "module" });
  const hub: Hub = { worker, channels: new Map() };
  worker.onmessage = (event: MessageEvent<ZarrHubReply>) => {
    const { channel, message } = event.data;
    // A reply for a channel closed while it was on its way is dropped.
    hub.channels.get(channel)?.onmessage?.({ data: message } as MessageEvent);
  };
  // A hub that fails to start fails every session on it.
  worker.onerror = (event) => {
    for (const channel of hub.channels.values()) channel.onerror?.(event);
  };
  return hub;
}

/**
 * A decode channel on a shared worker, for a session that reads a pinned
 * point: it speaks the protocol a dedicated worker speaks, but dozens of
 * them start no more threads than `hubLimit`. The hub with the fewest
 * channels takes the next one, and a new hub is started while there is room
 * for one and every hub already has a channel. A `columns` channel is hosted
 * the same way and speaks the column protocol instead.
 */
export function openZarrHubChannel(endpoint: ZarrHubEndpoint = "bundle"): DecodeChannel {
  let hub = hubs.reduce<Hub | null>(
    (least, candidate) => (least === null || candidate.channels.size < least.channels.size ? candidate : least),
    null,
  );
  if (hub === null || (hub.channels.size > 0 && hubs.length < hubLimit())) {
    hub = spawnHub();
    hubs.push(hub);
  }
  return new HubChannel(hub, nextHubChannel++, endpoint);
}
