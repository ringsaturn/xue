/** The live single-site radar end to end without a network: the session
 * over the live feed, a stubbed bucket and a stubbed decode worker. */

import { afterEach, describe, expect, it, vi } from "vitest";

import { liveFeed } from "../../web/src/radar/live";
import { BEAMS, GATES, type RadarProduct } from "../../web/src/radar/schema";
import { RadarSession } from "../../web/src/radar/session";

const NOW = Date.UTC(2026, 9, 6, 7, 30);

function stubBucket(): string[] {
  const asked: string[] = [];
  vi.stubGlobal("fetch", async (input: string | URL) => {
    const url = new URL(String(input));
    asked.push(url.searchParams.get("prefix") ?? url.pathname);
    const prefix = url.searchParams.get("prefix")!;
    const after = url.searchParams.get("start-after");
    // Two sweeps an hour, at :10 and :40.
    const keys = ["10_05", "40_05"].map((tail) => `${prefix}_${tail}`).filter((key) => !after || key > after);
    const body = `<ListBucketResult><IsTruncated>false</IsTruncated>${keys.map((key) => `<Contents><Key>${key}</Key></Contents>`).join("")}</ListBucketResult>`;
    return new Response(body, { status: 200 });
  });
  return asked;
}

class StubWorker {
  onmessage: ((event: MessageEvent) => void) | null = null;
  postMessage(request: { id: number; kind: string; product: RadarProduct; gates: number }): void {
    const codes = new Uint8Array(BEAMS * request.gates).fill(request.product === "n0g" ? 200 : 100);
    queueMicrotask(() => this.onmessage?.({ data: { id: request.id, ok: true, codes: codes.buffer, bytes: 1 } } as MessageEvent));
  }
}

async function settle(): Promise<void> {
  for (let turn = 0; turn < 20; turn++) await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("the live session", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  for (const product of ["n0b", "n0g"] as const) {
    it(`opens straight on ${product}`, async () => {
      vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
      const asked = stubBucket();
      vi.stubGlobal("Worker", StubWorker);
      const feed = liveFeed();
      const index = feed.sites.findIndex((site) => site.id === "HGX");
      const session = new RadarSession(feed.sites[index]!, (onChange) => feed.site(index, onChange), product, 1 << 30, () => {});
      session.setTime(Date.UTC(2026, 9, 6, 7, 20));
      await settle();
      expect(asked.filter((prefix) => prefix.startsWith(`HGX_${product.toUpperCase()}`))).toHaveLength(4);
      // 04:30–07:30: 04:40, then two an hour.
      expect(session.sweepTimes()).toHaveLength(7);
      const sweep = session.sweepAt(Date.UTC(2026, 9, 6, 7, 20));
      expect(sweep?.time).toBe(Date.UTC(2026, 9, 6, 7, 10, 5));
      expect(sweep?.codes.length).toBe(BEAMS * GATES[product]);
      session.close();
    });
  }
});
