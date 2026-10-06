/** The live single-site radar (`docs/nexrad.md` §7): the newest three hours
 * of a site's Level 3 sweeps, read straight out of the source bucket.
 *
 * The bucket is public, sends `Access-Control-Allow-Origin: *` on objects
 * and listings alike, and is flat — `<SITE>_<PRODUCT>_<YYYY_MM_DD_HH_MM_SS>`
 * with the sweep's own start in the key — so a site's timeline is a prefix
 * listing per hour and each sweep is one immutable object, decoded in the
 * browser. Nothing is built, stored or proxied on our side: only the sites
 * someone opens are ever read, at the source's own latency. */

import type { RadarFeed, RadarSiteFeed, RadarUnit } from "./feed";
import { BEAMS, GATES, type RadarProduct, type RadarSite } from "./schema";
import SITE_ROWS from "./sites.json";

export const LEVEL3_BUCKET_URL = "https://unidata-nexrad-level3.s3.amazonaws.com";
/** The window the live view keeps, as a case's is its interval. */
export const LIVE_WINDOW_SECONDS = 3 * 3600;
/** A sweep lands about 80 s after it starts and sites scan every 2.6–7
 * minutes: a minute's poll shows each within a minute of its arrival. */
const POLL_MS = 60_000;
const SOURCE_CODES: Record<RadarProduct, string> = { n0b: "N0B", n0g: "N0G" };
const KEY = /^([A-Z0-9]{3})_([A-Z0-9]{3})_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})$/;

/** Every WSR-88D site that publishes, from NCEI's station table
 * (`xue nexrad-sites` writes it): the marks the live view draws before any
 * request. */
export const LIVE_SITES: readonly RadarSite[] = (SITE_ROWS as [string, string, number, number, number][]).map(
  ([id, icao, lat, lon, height]) => ({ id, icao, lat, lon, height }),
);

/** A key's sweep start, epoch milliseconds; null for a key that is not a
 * Level 3 sweep's. */
export function keyTime(key: string): number | null {
  const match = KEY.exec(key);
  if (!match) return null;
  const [, , , year, month, day, hour, minute, second] = match;
  return Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second));
}

/** The listing prefix of one site, product and UTC hour. */
export function hourPrefix(site: string, product: RadarProduct, hour: number): string {
  const date = new Date(hour);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${site}_${SOURCE_CODES[product]}_${date.getUTCFullYear()}_${pad(date.getUTCMonth() + 1)}_${pad(date.getUTCDate())}_${pad(date.getUTCHours())}`;
}

/** The keys and continuation token of one ListObjectsV2 page. A regular
 * expression rather than DOMParser: the document is S3's fixed shape, and
 * the parse runs in tests without a DOM. */
export function parseListing(xml: string): { keys: string[]; next: string | null } {
  const keys = [...xml.matchAll(/<Key>([^<]*)<\/Key>/g)].map((match) => match[1]!);
  const truncated = /<IsTruncated>true<\/IsTruncated>/.test(xml);
  const next = truncated ? (/<NextContinuationToken>([^<]*)<\/NextContinuationToken>/.exec(xml)?.[1] ?? null) : null;
  return { keys, next };
}

/** Every key under `prefix` after `startAfter`, oldest first (the keys sort
 * by time within a site and product). */
async function listPrefix(prefix: string, startAfter: string | null, signal: AbortSignal): Promise<string[]> {
  const keys: string[] = [];
  let token: string | null = null;
  do {
    const url = new URL(LEVEL3_BUCKET_URL);
    url.searchParams.set("list-type", "2");
    url.searchParams.set("prefix", prefix);
    if (startAfter && startAfter.startsWith(prefix)) url.searchParams.set("start-after", startAfter);
    if (token) url.searchParams.set("continuation-token", token);
    const response = await fetch(url, { cache: "no-store", signal });
    if (!response.ok) throw new Error(`listing ${prefix} failed: ${response.status}`);
    const page = parseListing(await response.text());
    keys.push(...page.keys);
    token = page.next;
  } while (token);
  return keys;
}

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

interface Track {
  keys: { key: string; time: number }[];
  units: RadarUnit[] | null;
  timer: ReturnType<typeof setTimeout> | null;
  busy: boolean;
  listed: boolean;
}

class LiveSite implements RadarSiteFeed {
  private readonly tracks = new Map<RadarProduct, Track>();
  private readonly abort = new AbortController();
  private closed = false;
  private readonly shown = () => {
    if (!visible()) return;
    for (const product of this.tracks.keys()) void this.poll(product);
  };

  constructor(
    private readonly site: RadarSite,
    private readonly onChange: () => void,
  ) {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", this.shown);
  }

  units(product: RadarProduct): readonly RadarUnit[] {
    const track = this.tracks.get(product);
    if (!track) return [];
    track.units ??= track.keys.map(({ key, time }) => ({
      key,
      time,
      times: [time],
      bytes: BEAMS * GATES[product],
      request: { kind: "level3", url: `${LEVEL3_BUCKET_URL}/${key}`, product, gates: GATES[product] },
    }));
    return track.units;
  }

  watch(product: RadarProduct): void {
    if (this.closed || this.tracks.has(product)) return;
    this.tracks.set(product, { keys: [], units: null, timer: null, busy: false, listed: false });
    void this.poll(product);
  }

  close(): void {
    this.closed = true;
    this.abort.abort();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this.shown);
    for (const track of this.tracks.values()) if (track.timer) clearTimeout(track.timer);
    this.tracks.clear();
  }

  /** List what is new since the newest key held, drop what has aged out of
   * the window, and come back in a minute. A hidden tab only reschedules
   * once a product is listed; showing the tab again lists at once. */
  private async poll(product: RadarProduct): Promise<void> {
    const track = this.tracks.get(product);
    if (!track || track.busy || this.closed) return;
    if (track.timer) clearTimeout(track.timer);
    track.timer = null;
    track.busy = true;
    try {
      if (!track.listed || visible()) {
        await this.catchUp(product, track);
        track.listed = true;
      }
    } catch (error) {
      if (!this.closed) console.warn(`radar: ${this.site.id} ${product} listing failed:`, error instanceof Error ? error.message : error);
    } finally {
      track.busy = false;
      if (!this.closed) track.timer = setTimeout(() => void this.poll(product), POLL_MS);
    }
  }

  private async catchUp(product: RadarProduct, track: Track): Promise<void> {
    const now = Date.now();
    const start = now - LIVE_WINDOW_SECONDS * 1000;
    const newest = track.keys.at(-1) ?? null;
    const HOUR = 3600_000;
    const added: { key: string; time: number }[] = [];
    for (let hour = Math.floor(Math.max(start, newest?.time ?? start) / HOUR) * HOUR; hour <= now; hour += HOUR) {
      for (const key of await listPrefix(hourPrefix(this.site.id, product, hour), newest?.key ?? null, this.abort.signal)) {
        const time = keyTime(key);
        if (time !== null && time > start && (newest === null || time > newest.time)) added.push({ key, time });
      }
    }
    const kept = track.keys.filter((item) => item.time > start);
    if (added.length === 0 && kept.length === track.keys.length) return;
    track.keys = [...kept, ...added].sort((a, b) => a.time - b.time);
    track.units = null;
    this.onChange();
  }
}

/** The live source as a feed over the static site table. */
export function liveFeed(): RadarFeed {
  return {
    sites: LIVE_SITES,
    windowSeconds: LIVE_WINDOW_SECONDS,
    live: true,
    site: (index, onChange) => new LiveSite(LIVE_SITES[index]!, onChange),
  };
}
