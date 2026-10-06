/** Where a radar session's sweeps come from: the units it fetches, each
 * one decode request yielding one or more consecutive sweeps of one site
 * and product.
 *
 * Two feeds answer it. A window of polar stores (a showcase case,
 * `docs/nexrad.md` §4) gives one unit per round — one range out of the
 * round's shard holding every sweep of those five minutes. The live feed
 * (§7) gives one unit per Level 3 object in the source bucket, read whole
 * and decoded in the browser. The session above them only orders, budgets
 * and evicts units. */

import type { RadarDecodeRequest } from "./worker";
import { BEAMS, GATES, type RadarProduct, type RadarSite, type RadarWindow } from "./schema";

export interface RadarUnit {
  /** Stable for the unit's life: what the session keys residents by. */
  key: string;
  /** Where the unit stands on the clock, for eviction by distance. */
  time: number;
  /** Its sweeps' start times, epoch milliseconds, ascending. */
  times: number[];
  /** Decoded bytes it will hold resident. */
  bytes: number;
  request: RadarDecodeRequest;
}

/** One site's units of one product. A live site's units change as sweeps
 * arrive and age out; `onChange` says so. */
export interface RadarSiteFeed {
  /** A live site's newest sweep is fetched before anything else: it is
   * what the view shows past the window, and the first thing anyone
   * opening a live site looks at. */
  readonly live: boolean;
  units(product: RadarProduct): readonly RadarUnit[];
  /** The product is shown: a live feed starts listing it. */
  watch(product: RadarProduct): void;
  close(): void;
}

export interface RadarFeed {
  readonly sites: readonly RadarSite[];
  readonly windowSeconds: number;
  /** Whether the feed follows the clock (the live source) or is a closed
   * window (a case). */
  readonly live: boolean;
  site(index: number, onChange: () => void): RadarSiteFeed;
}

/** A window of polar stores as a feed: a site's unit per round is its inner
 * chunk in the round's store. */
export function windowFeed(window: RadarWindow, windowUrl: string): RadarFeed {
  return {
    sites: window.sites,
    windowSeconds: window.windowSeconds,
    live: false,
    site(index: number): RadarSiteFeed {
      const cache = new Map<RadarProduct, RadarUnit[]>();
      return {
        live: false,
        units(product) {
          let units = cache.get(product);
          if (units) return units;
          units = [];
          for (const round of window.rounds) {
            const block = round.products[product];
            const chunk = block?.chunks.find((item) => item.site === index);
            if (!block || !chunk) continue;
            const shard = new URL(`${product}.zarr/${product}/c/0/0/0/0`, new URL(round.path, windowUrl));
            shard.searchParams.set("v", block.shardCrc32);
            units.push({
              key: String(round.time),
              time: round.time,
              times: chunk.times,
              bytes: chunk.sweeps * BEAMS * GATES[product],
              request: {
                kind: "chunk",
                url: shard.href,
                offset: chunk.offset,
                length: chunk.length,
                scans: block.scans,
                sweeps: chunk.sweeps,
                gates: GATES[product],
              },
            });
          }
          cache.set(product, units);
          return units;
        },
        watch() {},
        close() {},
      };
    },
  };
}
