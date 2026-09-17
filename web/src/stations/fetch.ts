/** Reading the two station products off the data root: the pointer → index
 * fetch both share with the runs and the storm tracks (pointer with caching
 * disabled, index `?v=<crc32>` addressed and pointer-relative), and the one
 * range request that reads a single station out of the `.jsonl` beside the
 * index. Nothing here touches the map.
 *
 * A data root that publishes neither product is not a failure: the pointer
 * 404s and the loader resolves to null, the way `tc/tracks.ts` does, so a
 * shell deployed before the product exists asks once per poll and draws
 * nothing. */

import { fetchImmutable } from "../fetchimmutable";
import {
  AIRPORT_POINTER_FILENAME,
  SOUNDING_POINTER_FILENAME,
  parseAirportIndex,
  parseAirportPointer,
  parseAirportStation,
  parseSoundingIndex,
  parseSoundingPointer,
  parseSoundingStation,
  type AirportIndex,
  type AirportPointer,
  type AirportStation,
  type AirportStationHistory,
  type SoundingIndex,
  type SoundingPointer,
  type SoundingStation,
  type SoundingStationEntry,
  type StationFile,
} from "./schema";

export interface LoadedIndex<P, I> {
  pointer: P;
  index: I;
  /** Absolute index URL (with its `?v=`); the file beside it resolves
   * against this. */
  indexUrl: string;
}

export type LoadedSoundingIndex = LoadedIndex<SoundingPointer, SoundingIndex>;
export type LoadedAirportIndex = LoadedIndex<AirportPointer, AirportIndex>;

/** The pointer, then the index it names. Null when the root publishes no
 * such product (404, or a bucket that answers 403 for a missing key). */
async function loadIndex<P extends { path: string; crc32: string }, I>(
  baseUrl: string,
  filename: string,
  parsePointer: (input: unknown) => P,
  parseIndex: (input: unknown) => I,
  what: string,
): Promise<LoadedIndex<P, I> | null> {
  const pointerUrl = new URL(`${baseUrl}${filename}`, document.baseURI);
  const response = await fetch(pointerUrl, { cache: "no-cache" });
  if (response.status === 404 || response.status === 403) return null;
  if (!response.ok)
    throw new Error(`${what} pointer request failed: ${response.status}`);
  const pointer = parsePointer(await response.json());
  const indexUrl = new URL(pointer.path, new URL(baseUrl, document.baseURI));
  indexUrl.searchParams.set("v", pointer.crc32);
  const indexResponse = await fetchImmutable(indexUrl);
  if (!indexResponse.ok)
    throw new Error(`${what} index request failed: ${indexResponse.status}`);
  return {
    pointer,
    index: parseIndex(await indexResponse.json()),
    indexUrl: indexUrl.href,
  };
}

export function fetchSoundingIndex(baseUrl: string): Promise<LoadedSoundingIndex | null> {
  return loadIndex(
    baseUrl,
    SOUNDING_POINTER_FILENAME,
    parseSoundingPointer,
    parseSoundingIndex,
    "sounding",
  );
}

export function fetchAirportIndex(baseUrl: string): Promise<LoadedAirportIndex | null> {
  return loadIndex(
    baseUrl,
    AIRPORT_POINTER_FILENAME,
    parseAirportPointer,
    parseAirportIndex,
    "airport",
  );
}

/** The URL of the file beside an index, under the `?v=` the index gives
 * it. */
export function stationFileUrl(indexUrl: string, file: StationFile): string {
  const url = new URL(file.path, indexUrl);
  url.searchParams.set("v", file.crc32);
  return url.href;
}

/** The header one station's span asks for. Inclusive at both ends, which
 * is what makes `length` bytes of it. */
export function rangeHeader(offset: number, length: number): string {
  return `bytes=${offset}-${offset + length - 1}`;
}

/** The span a `Content-Range` header describes, or null when it is not one
 * this reader can check. */
export function parseContentRange(header: string | null): { offset: number; length: number } | null {
  const match = /^bytes (\d+)-(\d+)\/(?:\d+|\*)$/.exec((header ?? "").trim());
  if (!match) return null;
  const first = Number(match[1]);
  const last = Number(match[2]);
  if (last < first) return null;
  return { offset: first, length: last - first + 1 };
}

/** One station's bytes out of a `.jsonl`, in one request.
 *
 * A 206 is the everyday answer and must describe the span that was asked
 * for; a server that answers one range with another is serving the wrong
 * station, not a slow one, so it is an error rather than something to
 * work around. A 200 is a server (or a dev server) that ignores `Range`
 * and sent the whole file: the slice is taken from it at the same offsets,
 * which costs the download but reads the right station. Slicing is done on
 * the bytes, not on the decoded text: the offsets the index publishes are
 * byte offsets. */
async function fetchSpan(url: string, offset: number, length: number): Promise<unknown> {
  const response = await fetchImmutable(url, {
    headers: { Range: rangeHeader(offset, length) },
  });
  if (!response.ok)
    throw new Error(`station range request failed: ${response.status}`);
  const body = new Uint8Array(await response.arrayBuffer());
  let slice: Uint8Array;
  if (response.status === 206) {
    // Across origins the header is readable only when the bucket's CORS
    // policy exposes it, and the data origin's does not; a 206 the browser
    // shows without one is still the span asked for when it is exactly as
    // long, and the caller checks the id the slice names on top.
    const header = response.headers.get("Content-Range");
    if (header !== null) {
      const range = parseContentRange(header);
      if (range === null || range.offset !== offset || range.length !== length)
        throw new Error("station range response does not match the span asked for");
    }
    if (body.length !== length)
      throw new Error("station range response length mismatch");
    slice = body;
  } else {
    if (body.length < offset + length)
      throw new Error("station file is shorter than the span the index names");
    slice = body.subarray(offset, offset + length);
  }
  return JSON.parse(new TextDecoder().decode(slice)) as unknown;
}

/** One station's whole window of soundings, read by range out of
 * `soundings.jsonl`. */
export async function fetchSoundingStation(
  loaded: LoadedSoundingIndex,
  station: SoundingStationEntry,
): Promise<SoundingStation> {
  const url = stationFileUrl(loaded.indexUrl, loaded.index.soundings);
  const parsed = parseSoundingStation(await fetchSpan(url, station.offset, station.length));
  if (parsed.id !== station.id)
    throw new Error(`soundings.jsonl span names ${parsed.id}, not ${station.id}`);
  return parsed;
}

/** One airport's last 24 hours and current forecast, read by range out of
 * `history.jsonl`. */
export async function fetchAirportStation(
  loaded: LoadedAirportIndex,
  station: AirportStation,
): Promise<AirportStationHistory> {
  const url = stationFileUrl(loaded.indexUrl, loaded.index.history);
  const parsed = parseAirportStation(await fetchSpan(url, station.offset, station.length));
  if (parsed.icao !== station.icao)
    throw new Error(`history.jsonl span names ${parsed.icao}, not ${station.icao}`);
  return parsed;
}
