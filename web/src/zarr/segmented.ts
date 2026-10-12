/**
 * A rolling window's store cut along time, read as one.
 *
 * A window is published as segments (docs/zarr-profile.md, "Segmented
 * store"): ordinary stores, each covering one clock span of the axis, whose
 * axes concatenate into the bundle's. A segment that has filled its span is
 * uploaded once and named by every later round's manifest under the same
 * URL and `?v=`, so a round costs the bucket and a viewer only what changed.
 *
 * This file joins the segments back into the one bundle the rest of the
 * shell expects: the metadata is the segments' shared document with the
 * time axis rebuilt over every frame (`mergeSegmentMetadata`), and every
 * question the worker asks in the bundle's frame offsets is routed to the
 * segment that holds the frame, in that segment's own offsets.
 */

import { axisUnitSeconds, frameOffsets, HOUR_SECONDS, parseBundleMetadata } from "../manifest";
import type { TileRect } from "../tiles";
import { PayloadPool, ZarrSession, type ChunkKey, type ZarrReader } from "./session";
import type { ZarrStore } from "./store";

type DecodeChunkFn = Parameters<typeof ZarrSession.open>[1];

/** A chunk of one segment: the segment's own key plus which segment. */
export interface SegmentChunkKey extends ChunkKey {
  segment: number;
}

interface MergedAxis {
  /** The bundle's metadata: the segments' document with the joined axis. */
  metadataJson: string;
  /** Per segment, the bundle offset of each of its frames, in its order. */
  offsets: number[][];
}

function gcd(a: number, b: number): number {
  while (b !== 0) [a, b] = [b, a % b];
  return Math.abs(a);
}

/** The time block of an axis given as leads in seconds from the run time,
 * written the way the encoders write it (xuebuild `_time_metadata`): the
 * coarsest unit dividing the hour and every lead, then a `frameStep` when
 * the offsets are uniform and the list outright when they are not. */
export function canonicalTimeBlock(leads: readonly number[]): { block: Record<string, unknown>; offsets: number[] } {
  const unitSeconds = leads.reduce((unit, lead) => gcd(unit, lead), HOUR_SECONDS);
  const offsets = leads.map((lead) => lead / unitSeconds);
  const steps = new Set(offsets.slice(1).map((offset, index) => offset - offsets[index]!));
  const block: Record<string, unknown> = {
    unitSeconds,
    firstFrameOffset: offsets[0],
    frameCount: offsets.length,
  };
  if (steps.size <= 1) block.frameStep = steps.size === 1 ? [...steps][0] : 1;
  else block.frameOffsets = offsets;
  return { block, offsets };
}

/** Join the segments' metadata documents into the bundle's. Every segment
 * must carry the same document but for `runTime` and `time`, and their
 * frames must be strictly ascending across the segments; the joined
 * document keeps the first segment's `runTime` and key order. */
export function mergeSegmentMetadata(metadataJsons: readonly string[]): MergedAxis {
  if (metadataJsons.length === 0) throw new Error("a segmented store has no segments");
  const documents = metadataJsons.map((json) => JSON.parse(json) as Record<string, unknown>);
  const shared = (document: Record<string, unknown>) =>
    JSON.stringify(Object.fromEntries(Object.entries(document).filter(([key]) => key !== "runTime" && key !== "time")));
  const reference = shared(documents[0]!);
  if (documents.some((document) => shared(document) !== reference)) {
    throw new Error("the segments of a store do not describe one bundle");
  }
  const first = parseBundleMetadata(metadataJsons[0]!);
  const origin = Date.parse(first.runTime);
  const leads: number[][] = metadataJsons.map((json) => {
    const metadata = parseBundleMetadata(json);
    const start = Date.parse(metadata.runTime) - origin;
    const unit = axisUnitSeconds(metadata.time);
    return frameOffsets(metadata.time).map((offset) => start / 1000 + offset * unit);
  });
  const flat = leads.flat();
  for (let index = 1; index < flat.length; index += 1) {
    if (flat[index]! <= flat[index - 1]!) throw new Error("the segments of a store are not in time order");
  }
  if (flat[0]! < 0 || !flat.every(Number.isInteger)) throw new Error("the segments of a store are not on one clock");
  const { block, offsets } = canonicalTimeBlock(flat);
  const joined: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(documents[0]!)) joined[key] = key === "time" ? block : value;
  const metadataJson = JSON.stringify(joined);
  // The joined axis is held to the same rules as any bundle's (offset
  // range, canonical form), so a window that outgrows them fails here.
  parseBundleMetadata(metadataJson);
  let cursor = 0;
  return {
    metadataJson,
    offsets: leads.map((segment) => segment.map(() => offsets[cursor++]!)),
  };
}

export class SegmentedZarrSession implements ZarrReader {
  private readonly located = new Map<number, { segment: number; offset: number }>();

  private constructor(
    private readonly segments: ZarrSession[],
    readonly metadataJson: string,
    offsets: number[][],
  ) {
    offsets.forEach((segmentOffsets, segment) => {
      const local = segments[segment]!.frameOffsets;
      segmentOffsets.forEach((offset, index) => this.located.set(offset, { segment, offset: local[index]! }));
    });
  }

  /** Open every segment and join them. The payload budget is one pool over
   * every segment (`PayloadPool`), so the segment being played may use all
   * of it. */
  static async open(
    stores: readonly ZarrStore[],
    decodeChunk: DecodeChunkFn,
    options: { payloadBudgetBytes?: number } = {},
  ): Promise<SegmentedZarrSession> {
    if (stores.length === 0) throw new Error("a segmented store has no segments");
    const budget = options.payloadBudgetBytes;
    const pool = budget !== undefined && budget > 0 ? new PayloadPool(budget) : undefined;
    const segments = await Promise.all(
      stores.map((store) => ZarrSession.open(store, decodeChunk, pool ? { payloadPool: pool } : {})),
    );
    const first = segments[0]!;
    const geometry = first.tileGeometry().join(",");
    const ids = first.numericIds.join(",");
    for (const segment of segments) {
      if (segment.tileGeometry().join(",") !== geometry || segment.numericIds.join(",") !== ids) {
        throw new Error("the segments of a store must be cut the same way");
      }
    }
    const merged = mergeSegmentMetadata(segments.map((segment) => segment.metadataJson));
    return new SegmentedZarrSession(segments, merged.metadataJson, merged.offsets);
  }

  get planeLength(): number {
    return this.segments[0]!.planeLength;
  }

  get numericIds(): number[] {
    return this.segments[0]!.numericIds;
  }

  get frameOffsets(): number[] {
    return [...this.located.keys()];
  }

  get fetchedBytes(): number {
    return this.segments.reduce((sum, segment) => sum + segment.fetchedBytes, 0);
  }

  get residentBytes(): number {
    return this.segments.reduce((sum, segment) => sum + segment.residentBytes, 0);
  }

  get payloadBytes(): number {
    return this.segments.reduce((sum, segment) => sum + segment.payloadBytes, 0);
  }

  tileGeometry(): Uint32Array {
    return this.segments[0]!.tileGeometry();
  }

  clearCache(): void {
    for (const segment of this.segments) segment.clearCache();
  }

  private locate(frameOffset: number): { segment: number; offset: number } {
    const place = this.located.get(frameOffset);
    if (!place) throw new Error("no plane for the requested variable and forecast hour");
    return place;
  }

  chunksFor(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): SegmentChunkKey[] {
    const { segment, offset } = this.locate(frameOffset);
    return this.segments[segment]!.chunksFor(variableId, offset, rects).map((key) => ({ ...key, segment }));
  }

  isResident(key: SegmentChunkKey): boolean {
    return this.segments[key.segment]!.isResident(key);
  }

  isFetching(key: SegmentChunkKey): boolean {
    return this.segments[key.segment]!.isFetching(key);
  }

  ensureChunks(keys: readonly SegmentChunkKey[]): Promise<void> {
    return Promise.all(keys.map((key) => this.segments[key.segment]!.ensureChunk(key))).then(() => undefined);
  }

  frameResident(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): boolean {
    const { segment, offset } = this.locate(frameOffset);
    return this.segments[segment]!.frameResident(variableId, offset, rects);
  }

  async decodeFrame(variableId: number, frameOffset: number, rects: readonly TileRect[] | null = null): Promise<Uint8Array> {
    const { segment, offset } = this.locate(frameOffset);
    return this.segments[segment]!.decodeFrame(variableId, offset, rects);
  }

  /** One cell's codes on every frame of the joined axis, segment after
   * segment. */
  async decodeSeries(variableId: number, column: number, row: number): Promise<Uint8Array> {
    const parts = await Promise.all(this.segments.map((segment) => segment.decodeSeries(variableId, column, row)));
    const series = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
    let at = 0;
    for (const part of parts) {
      series.set(part, at);
      at += part.length;
    }
    return series;
  }
}
