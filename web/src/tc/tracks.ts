/** Reading the product and walking a track in time: the pointer → index
 * → storm fetches (the run pointer's own contract, `?v=<crc32>` addressed
 * and manifest-relative), a position along a forecast at any valid time,
 * and the little spherical geometry the wind radii need. Nothing here
 * touches the map. */

import { fetchImmutable } from "../fetchimmutable";
import {
  ensembleMembers,
  isTcEnsemble,
  TC_POINTER_FILENAME,
  type TcEnsemble,
  type TcForecast,
  type TcIndex,
  type TcIndexEntry,
  type TcPoint,
  type TcPointer,
  type TcRadii,
  type TcStorm,
  validateTcIndex,
  validateTcPointer,
  validateTcStorm,
} from "./schema";

export interface LoadedTcIndex {
  pointer: TcPointer;
  index: TcIndex;
  /** Absolute index URL (with its `?v=`); storm paths resolve against it. */
  indexUrl: string;
}

/** The live product, or null when the data root publishes none (a 404 on
 * the pointer is the product not existing, not a failure). */
export async function fetchTcIndex(
  baseUrl: string,
): Promise<LoadedTcIndex | null> {
  const pointerUrl = new URL(
    `${baseUrl}${TC_POINTER_FILENAME}`,
    document.baseURI,
  );
  const response = await fetch(pointerUrl, { cache: "no-cache" });
  if (response.status === 404 || response.status === 403) return null;
  if (!response.ok)
    throw new Error(`tc pointer request failed: ${response.status}`);
  const pointer = validateTcPointer(await response.json());
  const indexUrl = new URL(pointer.path, new URL(baseUrl, document.baseURI));
  indexUrl.searchParams.set("v", pointer.crc32);
  const indexResponse = await fetchImmutable(indexUrl);
  if (!indexResponse.ok)
    throw new Error(`tc index request failed: ${indexResponse.status}`);
  return {
    pointer,
    index: validateTcIndex(await indexResponse.json()),
    indexUrl: indexUrl.href,
  };
}

export async function fetchTcStorm(
  loaded: LoadedTcIndex,
  entry: TcIndexEntry,
): Promise<TcStorm> {
  const url = new URL(entry.path, loaded.indexUrl);
  url.searchParams.set("v", entry.crc32);
  const response = await fetchImmutable(url);
  if (!response.ok)
    throw new Error(`tc storm request failed: ${response.status}`);
  const storm = validateTcStorm(await response.json());
  if (storm.id !== entry.id)
    throw new Error(
      `tc storm file ${entry.path} names ${storm.id}, not ${entry.id}`,
    );
  return storm;
}

/** The id a link names, followed through the crosswalk: a storm renamed
 * since the link was made still opens. */
export function resolveTcStormId(index: TcIndex, id: string): string | null {
  let current = id;
  for (let hops = 0; hops < 8; hops += 1) {
    if (index.storms.some((entry) => entry.id === current)) return current;
    const next = index.crosswalk[current];
    if (next === undefined) return null;
    current = next;
  }
  return null;
}

export type LonLat = [number, number];

const EARTH_RADIUS_KM = 6371;
const DEGREES = 180 / Math.PI;
const RADIANS = Math.PI / 180;

/** The point `km` along `bearing` (degrees clockwise from north). */
export function destination(
  lat: number,
  lon: number,
  bearing: number,
  km: number,
): LonLat {
  const delta = km / EARTH_RADIUS_KM;
  const phi = lat * RADIANS;
  const lambda = lon * RADIANS;
  const theta = bearing * RADIANS;
  const sinPhi =
    Math.sin(phi) * Math.cos(delta) +
    Math.cos(phi) * Math.sin(delta) * Math.cos(theta);
  const phi2 = Math.asin(sinPhi);
  const lambda2 =
    lambda +
    Math.atan2(
      Math.sin(theta) * Math.sin(delta) * Math.cos(phi),
      Math.cos(delta) - Math.sin(phi) * sinPhi,
    );
  return [lambda2 * DEGREES, phi2 * DEGREES];
}

/** Longitudes made continuous — each within 180° of the one before, so a
 * track across the antimeridian is one line the map draws in the
 * neighbouring world copy instead of a stroke around the globe. */
export function unwrapLongitudes(coordinates: LonLat[]): LonLat[] {
  const result: LonLat[] = [];
  let offset = 0;
  let previous: number | null = null;
  for (const [lon, lat] of coordinates) {
    let current = lon + offset;
    if (previous !== null) {
      if (current - previous > 180) {
        offset -= 360;
        current -= 360;
      } else if (previous - current > 180) {
        offset += 360;
        current += 360;
      }
    }
    result.push([current, lat]);
    previous = current;
  }
  return result;
}

export function pointTime(point: TcPoint): number {
  return Date.parse(point.time);
}

export interface TrackPosition {
  lon: number;
  lat: number;
  vmax: number | null;
  pmin: number | null;
  radii: TcRadii | null;
  class: string | null;
  /** Index of the last point at or before the time; -1 before the first. */
  index: number;
  /** 0..1 between that point and the next. */
  fraction: number;
}

function mix(
  a: number | null,
  b: number | null,
  fraction: number,
): number | null {
  if (a === null || b === null) return fraction < 0.5 ? a : b;
  return a + (b - a) * fraction;
}

function mixRadii(
  a: TcRadii | null,
  b: TcRadii | null,
  fraction: number,
): TcRadii | null {
  if (a === null || b === null) return fraction < 0.5 ? a : b;
  const result: TcRadii = {};
  for (const threshold of ["34", "50", "64"] as const) {
    const from = a[threshold];
    const to = b[threshold];
    if (!from || !to) {
      const kept = fraction < 0.5 ? from : to;
      if (kept) result[threshold] = kept;
      continue;
    }
    result[threshold] = from.map((value, index) =>
      mix(value, to[index] ?? null, fraction),
    );
  }
  return result;
}

/** Where a track is at `time` (ms since epoch): the linear interpolation
 * between its bracketing points, the longitude unwrapped across the
 * antimeridian. Null outside the track's span. */
export function positionAt(
  points: readonly TcPoint[],
  time: number,
): TrackPosition | null {
  if (points.length === 0) return null;
  const first = pointTime(points[0]!);
  if (time < first) return null;
  const last = pointTime(points[points.length - 1]!);
  if (time > last) return null;
  let index = 0;
  while (index + 1 < points.length && pointTime(points[index + 1]!) <= time)
    index += 1;
  const a = points[index]!;
  if (index + 1 >= points.length) {
    return {
      lon: a.lon,
      lat: a.lat,
      vmax: a.vmax,
      pmin: a.pmin,
      radii: a.radii,
      class: a.class,
      index,
      fraction: 0,
    };
  }
  const b = points[index + 1]!;
  const span = pointTime(b) - pointTime(a);
  const fraction = span > 0 ? (time - pointTime(a)) / span : 0;
  let lonB = b.lon;
  if (lonB - a.lon > 180) lonB -= 360;
  else if (a.lon - lonB > 180) lonB += 360;
  return {
    lon: a.lon + (lonB - a.lon) * fraction,
    lat: a.lat + (b.lat - a.lat) * fraction,
    vmax: mix(a.vmax, b.vmax, fraction),
    pmin: mix(a.pmin, b.pmin, fraction),
    radii: mixRadii(a.radii, b.radii, fraction),
    class: fraction < 0.5 ? a.class : b.class,
    index,
    fraction,
  };
}

/** A wind-radii ring: four quadrant arcs (NE, SE, SW, NW — bearings 0–90,
 * 90–180, 180–270, 270–360) at their own radii, sampled every 10°, joined
 * through the centre where a quadrant is missing or zero. Null when no
 * quadrant has a radius. */
export function radiiRing(
  lat: number,
  lon: number,
  quadrants: readonly (number | null)[],
): LonLat[] | null {
  if (!quadrants.some((radius) => radius !== null && radius > 0)) return null;
  const ring: LonLat[] = [];
  for (let quadrant = 0; quadrant < 4; quadrant += 1) {
    const radius = quadrants[quadrant] ?? null;
    if (radius === null || radius <= 0) {
      ring.push([lon, lat]);
      continue;
    }
    for (let step = 0; step <= 9; step += 1) {
      ring.push(destination(lat, lon, quadrant * 90 + step * 10, radius));
    }
  }
  ring.push(ring[0]!);
  return ring;
}

/** The forecast as a line: `[lon, lat]` per point, unwrapped. */
export function forecastLine(points: readonly TcPoint[]): LonLat[] {
  return unwrapLongitudes(points.map((point) => [point.lon, point.lat]));
}

/** The lines of every member of an ensemble that has more than one point,
 * plus the mean when the source published one. */
export function ensembleLines(
  ensemble: TcEnsemble,
): { member: number; line: LonLat[] }[] {
  return ensembleMembers(ensemble)
    .filter((track) => track.lat.length > 1)
    .map((track) => ({
      member: track.member,
      line: unwrapLongitudes(track.lat.map((lat, i) => [track.lon[i]!, lat])),
    }));
}

/** The deterministic tracks of a storm's models — a model's own track, or
 * an ensemble's mean when it has one. */
export function modelTracks(
  storm: TcStorm,
): { model: string; forecast: TcForecast; fromEnsemble: boolean }[] {
  const result = [];
  for (const [model, value] of Object.entries(storm.models)) {
    if (isTcEnsemble(value)) {
      if (value.mean)
        result.push({ model, forecast: value.mean, fromEnsemble: true });
    } else {
      result.push({ model, forecast: value, fromEnsemble: false });
    }
  }
  return result;
}

/** The valid-time span the storm's lines cover, for framing it. */
export function stormBounds(
  storm: TcStorm,
): [number, number, number, number] | null {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  const take = (points: readonly TcPoint[]) => {
    for (const [lon, lat] of forecastLine(points)) {
      west = Math.min(west, lon);
      east = Math.max(east, lon);
      south = Math.min(south, lat);
      north = Math.max(north, lat);
    }
  };
  for (const track of Object.values(storm.best)) take(track.points);
  for (const forecast of Object.values(storm.agencies)) take(forecast.points);
  for (const { forecast } of modelTracks(storm)) take(forecast.points);
  if (!Number.isFinite(west)) return null;
  return [west, south, east, north];
}
