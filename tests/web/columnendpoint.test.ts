/** The column channel's messages, over the committed fixture. */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../web/src/zarr/wasm", () => ({ loadWasm: async () => {} }));

import { createColumnEndpoint } from "../../web/src/zarr/columnendpoint";
import { localFetch } from "../../web/tooling/localfetch";

function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

const ROOT = join(repositoryRoot(), "tests/fixtures/column");

beforeAll(async () => {
  const wasm = await import("../../web/src/wasm/xue");
  await wasm.default({ module_or_path: readFileSync(join(repositoryRoot(), "web/src/wasm/xue_bg.wasm")) });
});

beforeEach(() => {
  vi.stubGlobal("fetch", localFetch(ROOT));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

async function opened() {
  const posted: Record<string, unknown>[] = [];
  const endpoint = createColumnEndpoint((message) => posted.push(message as Record<string, unknown>));
  await endpoint.handle({ type: "init-columns", url: "local://columns.zarr", crc32: "deadbeef", heldBytes: 0 });
  return { endpoint, posted };
}

describe("a column channel", () => {
  it("answers an open with the block and its shape", async () => {
    const { posted } = await opened();
    expect(posted).toHaveLength(1);
    expect(posted[0]).toMatchObject({ type: "ready", blockShape: [8, 8] });
    expect(JSON.parse(String(posted[0]!.blockJson))).toHaveProperty("arrays");
  });

  it("answers cells a block at a time, the last one saying so", async () => {
    const { endpoint, posted } = await opened();
    const cells = Uint32Array.from([0, 0, 24, 17, 7, 7, 60, 12]);
    await endpoint.handle({ type: "cells", requestId: 9, array: "tmp", cells });
    const replies = posted.slice(1);
    expect(replies).toHaveLength(3);
    expect(replies.every((reply) => reply.type === "cells" && reply.requestId === 9)).toBe(true);
    expect(replies.map((reply) => reply.done)).toEqual([false, false, true]);
    expect(replies.flatMap((reply) => [...(reply.indices as Uint32Array)]).sort()).toEqual([0, 1, 2, 3]);
    for (const reply of replies) {
      // Seven frames of three levels.
      expect((reply.buffer as ArrayBuffer).byteLength).toBe((reply.indices as Uint32Array).length * 21);
    }
    await endpoint.handle({ type: "cells", requestId: 10, array: "tmp", cells: new Uint32Array(0) });
    expect(posted.at(-1)).toMatchObject({ type: "cells", requestId: 10, done: true });
  });

  it("names the read that failed, and does not answer one that was let go", async () => {
    const { endpoint, posted } = await opened();
    await endpoint.handle({ type: "cells", requestId: 3, array: "none", cells: Uint32Array.from([0, 0]) });
    expect(posted.at(-1)).toMatchObject({ type: "error", requestId: 3 });
    const before = posted.length;
    const running = endpoint.handle({ type: "cells", requestId: 4, array: "tmp", cells: Uint32Array.from([0, 0]) });
    await endpoint.handle({ type: "supersede" });
    await running;
    expect(posted).toHaveLength(before);
  });
});
