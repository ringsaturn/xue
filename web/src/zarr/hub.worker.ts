/// <reference lib="webworker" />
/**
 * The Zarr hub worker: many decode channels on one thread.
 *
 * A pinned point reads one cell's series out of every bundle its panel
 * shows — the meteogram's rows, and with a model column on the chart four
 * bundles for each pressure level. Each of those is a session speaking the
 * decode protocol, and a dedicated worker apiece (`worker.ts`) is some forty
 * threads and forty WASM instances started together, which a phone answers
 * by not answering taps for a while. Their work is a few range reads and a
 * few chunk decodes each, after which they sit idle: it fits on a handful
 * of threads.
 *
 * So a hub hosts any number of endpoints (`endpoint.ts`, the same loop a
 * dedicated worker runs), each under the channel number the main thread
 * opened it with. Every message in either direction is an envelope naming
 * its channel (`ZarrHubRequest`, `ZarrHubReply`), and inside the envelope
 * the protocol is the one `main.ts` already speaks — `channel.ts` hands it
 * a `DecodeChannel` that hides the envelope, so a session cannot tell a hub
 * from a worker of its own.
 */

import { createZarrEndpoint, type ZarrEndpoint } from "./endpoint";
import type { ZarrHubReply, ZarrHubRequest } from "./protocol";

const scope = self as unknown as DedicatedWorkerGlobalScope;
const endpoints = new Map<number, ZarrEndpoint>();

function reply(message: ZarrHubReply, transfer: Transferable[] = []): void {
  scope.postMessage(message, transfer);
}

self.onmessage = (event: MessageEvent<ZarrHubRequest>) => {
  const request = event.data;
  if (request.kind === "open") {
    const { channel } = request;
    endpoints.set(
      channel,
      createZarrEndpoint((message, transfer = []) => reply({ channel, message }, transfer)),
    );
    // What a dedicated worker says on load, said for the channel.
    reply({ channel, message: { type: "booted" } });
    return;
  }
  if (request.kind === "close") {
    // Whatever the endpoint still has in flight answers into a channel the
    // main thread has already forgotten.
    endpoints.get(request.channel)?.close();
    endpoints.delete(request.channel);
    return;
  }
  void endpoints.get(request.channel)?.handle(request.message);
};
