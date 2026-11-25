/**
 * A column store's channel on a hub worker: the few messages `columns.ts` is
 * driven by (`protocol.ts`, `ColumnWorkerRequest`). A read that fails is
 * answered with an `error` naming it, one that was let go is not answered.
 */

import { WasmColumnStore } from "../wasm/xue";
import { ColumnSession } from "./columns";
import type { ZarrPost } from "./endpoint";
import type { ColumnCellsMessage, ColumnCellsReply, ColumnInitMessage, ColumnReadMessage, ColumnWorkerRequest } from "./protocol";
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
    const store = new ZarrStore(message.url, message.crc32);
    const opened = await ColumnSession.open(store, { WasmColumnStore }, message.heldBytes);
    if (closed) {
      // Closed while the store was opening: nobody is left to read it.
      opened.dispose();
      return;
    }
    session = opened;
    post({ type: "ready", blockJson: opened.blockJson, blockShape: opened.blockShape });
  }

  async function read(message: ColumnReadMessage): Promise<void> {
    if (!session) throw new Error("column store is not initialized");
    const codes = await session.read(message.array, message.row, message.column);
    if (codes === null) return;
    // The column is the reader's own new array: its buffer is posted.
    const buffer = codes.buffer as ArrayBuffer;
    post({ type: "column", requestId: message.requestId, buffer }, [buffer]);
  }

  async function readCells(message: ColumnCellsMessage): Promise<void> {
    if (!session) throw new Error("column store is not initialized");
    const { requestId, array, cells } = message;
    const count = cells.length / 2;
    let answered = 0;
    const answer = (indices: Uint32Array, codes: Uint8Array): void => {
      answered += indices.length;
      const buffer = codes.buffer as ArrayBuffer;
      const reply: ColumnCellsReply = { type: "cells", requestId, indices, buffer, done: answered === count };
      post(reply, [indices.buffer as ArrayBuffer, buffer]);
    };
    if (count === 0) answer(new Uint32Array(0), new Uint8Array(0));
    else await session.readCells(array, cells, answer);
  }

  async function handle(message: ColumnWorkerRequest): Promise<void> {
    if (closed) return;
    try {
      if (message.type === "init-columns") await init(message);
      else if (message.type === "column") await read(message);
      else if (message.type === "cells") await readCells(message);
      else session?.supersede();
    } catch (error) {
      if (closed) return;
      post({
        type: "error",
        requestId: "requestId" in message ? message.requestId : undefined,
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
