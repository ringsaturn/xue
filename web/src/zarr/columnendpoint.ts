/**
 * A column store's channel on a hub worker: the few messages `columns.ts` is
 * driven by (`protocol.ts`, `ColumnWorkerRequest`). A read that fails is
 * answered with an `error` naming it, one that was let go is not answered.
 */

import { WasmColumnStore } from "../wasm/xue";
import { ColumnSession } from "./columns";
import type { ZarrPost } from "./endpoint";
import type { ColumnInitMessage, ColumnReadMessage, ColumnWorkerRequest } from "./protocol";
import { ZarrStore } from "./store";
import { loadWasm } from "./wasm";

export interface ColumnEndpoint {
  /** Answer one request. Never rejects: a failure is posted. */
  handle(message: ColumnWorkerRequest): Promise<void>;
  close(): void;
}

export function createColumnEndpoint(post: ZarrPost): ColumnEndpoint {
  let session: ColumnSession | null = null;
  let closed = false;

  async function init(message: ColumnInitMessage): Promise<void> {
    await loadWasm();
    const opened = await ColumnSession.open(new ZarrStore(message.url, message.crc32), { WasmColumnStore });
    if (closed) {
      // Closed while the store was opening: nobody is left to read it.
      opened.dispose();
      return;
    }
    session = opened;
    post({ type: "ready", blockJson: opened.blockJson });
  }

  async function read(message: ColumnReadMessage): Promise<void> {
    if (!session) throw new Error("column store is not initialized");
    const codes = await session.read(message.array, message.row, message.column);
    if (codes === null) return;
    // The column is the reader's own new array: its buffer is posted.
    const buffer = codes.buffer as ArrayBuffer;
    post({ type: "column", requestId: message.requestId, buffer }, [buffer]);
  }

  async function handle(message: ColumnWorkerRequest): Promise<void> {
    if (closed) return;
    try {
      if (message.type === "init-columns") await init(message);
      else if (message.type === "column") await read(message);
      else session?.supersede();
    } catch (error) {
      if (closed) return;
      post({
        type: "error",
        requestId: message.type === "column" ? message.requestId : undefined,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return {
    handle,
    close() {
      closed = true;
      session?.dispose();
      session = null;
    },
  };
}
