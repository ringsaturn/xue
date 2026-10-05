/** The single-site radar's window manifest (`docs/nexrad.md` §4), schema v1,
 * as the shell reads it: the site table, and per round and product the
 * polar store's `?v=`, each site's byte span in the shard and its sweep
 * times.
 *
 * Admission is structural, the posture the other point products take: an
 * unknown site or source id is data. What is checked is what a reader
 * leans on — the spans lie in order inside the shard the round measured,
 * every site index names a row of the table, and a site's sweeps are as
 * many as its span says and strictly later than the window's before them.
 * A reader that slices a shard by these spans must be able to trust them. */

import { CRC32, integer, object, pointer, schemaVersion, string, timestamp, type ProductPointer } from "../schema/validate";

const SCHEMA_VERSION = 1;
export const NEXRAD_POINTER_FILENAME = "latest-nexrad.json";
const POINTER_PATH = /^nexrad\.\d{12}\/index\.json$/;
const SITE = /^[A-Z0-9]{3}$/;
const ICAO = /^[A-Z0-9]{4}$/;
const ROUND_PATH = /^\.\.\/nexrad\.\d{12}\/$/;

export const RADAR_PRODUCTS = ["n0b", "n0g"] as const;
export type RadarProduct = (typeof RADAR_PRODUCTS)[number];

/** Beam and gate geometry, the same for every store of a product
 * (`docs/nexrad.md` §2): 720 beams of 0.5° from true north, gates of
 * 0.25 km from the antenna. */
export const BEAMS = 720;
export const GATE_KM = 0.25;
export const GATES: Record<RadarProduct, number> = { n0b: 1840, n0g: 1200 };

export type NexradPointer = ProductPointer<"nexrad">;

export interface RadarSite {
  id: string;
  icao: string;
  lat: number;
  lon: number;
  /** Antenna height above mean sea level, metres. */
  height: number;
}

/** One site's inner chunk in one round's store of one product. */
export interface RadarChunk {
  site: number;
  offset: number;
  length: number;
  /** Real sweeps at the head of the chunk; the rest is padding. */
  sweeps: number;
  /** Their start times, epoch milliseconds, ascending. */
  times: number[];
}

export interface RadarRoundProduct {
  /** The store root document's CRC32, its `?v=`. */
  groupCrc32: string;
  shardBytes: number;
  shardCrc32: string;
  /** Sweep slots per chunk: the store's padded scan count, which a chunk
   * decompresses to `scans × 720 × gates` bytes of. */
  scans: number;
  chunks: RadarChunk[];
}

export interface RadarRound {
  /** The round's minute, epoch milliseconds. */
  time: number;
  /** The round's directory, relative to the manifest. */
  path: string;
  products: Partial<Record<RadarProduct, RadarRoundProduct>>;
}

export interface RadarWindow {
  schemaVersion: number;
  issued: number;
  windowSeconds: number;
  sites: RadarSite[];
  rounds: RadarRound[];
}

function list(input: unknown, label: string): unknown[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input;
}

function number(value: unknown, label: string, low: number, high: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < low || value > high)
    throw new Error(`${label} must be a number in ${low}..${high}`);
  return value;
}

function file(input: unknown, label: string): { byteLength: number; crc32: string } {
  const value = object(input, label);
  return {
    byteLength: integer(value.byteLength, `${label}.byteLength`, 1),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

export function parseNexradPointer(input: unknown): NexradPointer {
  return pointer(input, "nexrad", POINTER_PATH, SCHEMA_VERSION);
}

export function parseRadarWindow(input: unknown): RadarWindow {
  const value = object(input, "radar window");
  const version = schemaVersion(value.schemaVersion, "radar window", SCHEMA_VERSION);
  const issued = Date.parse(timestamp(value.issued, "window.issued"));
  const windowSeconds = integer(value.windowSeconds, "window.windowSeconds", 1);
  const seenSites = new Set<string>();
  const sites = list(value.sites, "window.sites").map((item, index) => {
    const label = `window.sites[${index}]`;
    const row = list(item, label);
    if (row.length !== 5) throw new Error(`${label} must be [id, icao, latitude, longitude, height]`);
    const id = string(row[0], `${label}.id`, SITE);
    if (seenSites.has(id)) throw new Error(`${label}.id repeats ${id}`);
    seenSites.add(id);
    return {
      id,
      icao: string(row[1], `${label}.icao`, ICAO),
      lat: number(row[2], `${label}.latitude`, -90, 90),
      lon: number(row[3], `${label}.longitude`, -180, 180),
      height: number(row[4], `${label}.height`, -500, 9000),
    } satisfies RadarSite;
  });
  const latest = new Map<string, number>();
  let previousRound = -Infinity;
  const rounds = list(value.rounds, "window.rounds").map((item, index) => {
    const label = `window.rounds[${index}]`;
    const entry = object(item, label);
    const time = Date.parse(timestamp(entry.round, `${label}.round`));
    if (time <= previousRound) throw new Error("window.rounds must be oldest first and unique");
    if (time > issued || time <= issued - windowSeconds * 1000) throw new Error(`${label} is outside the window`);
    previousRound = time;
    const path = string(entry.path, `${label}.path`, ROUND_PATH);
    const products: Partial<Record<RadarProduct, RadarRoundProduct>> = {};
    for (const product of RADAR_PRODUCTS) {
      if (entry[product] === undefined) continue;
      const where = `${label}.${product}`;
      const block = object(entry[product], where);
      const group = file(block.group, `${where}.group`);
      const shard = file(block.shard, `${where}.shard`);
      const scans = list(block.scans, `${where}.scans`);
      const rows = list(block.chunks, `${where}.chunks`);
      if (rows.length === 0 || rows.length !== scans.length)
        throw new Error(`${where} needs a chunk and a scan row per site`);
      let end = 0;
      let padded = 0;
      const chunks = rows.map((row, position) => {
        const chunk = list(row, `${where}.chunks[${position}]`);
        if (chunk.length !== 4) throw new Error(`${where}.chunks rows are [site, offset, length, sweeps]`);
        const site = integer(chunk[0], `${where} site`);
        if (site >= sites.length) throw new Error(`${where} names a site past the table`);
        const offset = integer(chunk[1], `${where} offset`);
        const length = integer(chunk[2], `${where} length`, 1);
        const sweeps = integer(chunk[3], `${where} sweeps`, 1);
        if (offset < end || offset + length > shard.byteLength)
          throw new Error(`${where} spans must be in order inside the shard`);
        end = offset + length;
        const scan = list(scans[position], `${where}.scans[${position}]`);
        if (scan.length !== 2 || scan[0] !== site) throw new Error(`${where}.scans rows follow the chunks`);
        const times = list(scan[1], `${where}.scans[${position}][1]`).map((seconds) => integer(seconds, `${where} scan time`) * 1000);
        if (times.length !== sweeps) throw new Error(`${where} site ${site}: sweeps and times disagree`);
        const key = `${product}:${site}`;
        for (const time of times) {
          if (time > (latest.get(key) ?? -Infinity) && time <= previousRound) latest.set(key, time);
          else throw new Error(`${where} site ${site}: sweeps must strictly follow the window's`);
        }
        padded = Math.max(padded, sweeps);
        return { site, offset, length, sweeps, times } satisfies RadarChunk;
      });
      products[product] = {
        groupCrc32: group.crc32,
        shardBytes: shard.byteLength,
        shardCrc32: shard.crc32,
        // A store pads every site to its busiest one (the polar profile's
        // inner chunk), and the manifest lists every site of the store, so
        // the largest sweep count here is the chunk's padded length exactly.
        scans: padded,
        chunks,
      };
    }
    if (Object.keys(products).length === 0) throw new Error(`${label} holds no product`);
    return { time, path, products } satisfies RadarRound;
  });
  return { schemaVersion: version, issued, windowSeconds, sites, rounds };
}
