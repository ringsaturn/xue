/** The station nearest a pinned point, one product at a time.
 *
 * A pin on the map is a question about a place, and the two point products
 * answer a part of it the runs cannot: what a sonde measured through the
 * column this morning, and what the aerodrome down the road has actually
 * been reporting. Neither has to be switched on as marks for that — the
 * marks are a map layer and this is a reading — so the panel resolves both
 * from whatever indexes have loaded, whether or not a rail tile is pressed.
 *
 * The two radii differ because the two products do. A radiosonde ascent is
 * a synoptic-scale profile and stands in for a wide region, so 150 km is
 * still the same air mass; an airport observation is a point measurement of
 * a runway, and past 40 km it is somebody else's weather. Both are ceilings,
 * not preferences: nothing is offered beyond them rather than a distant
 * station being offered faintly.
 *
 * Arithmetic only — no DOM, no fetching. The index is read, never mutated.
 */

import type { AirportIndex, AirportStation, SoundingIndex, SoundingStationEntry } from "./schema";

/** A station and how far the pinned point is from it. */
export interface NearestStation<S> {
  station: S;
  /** Great-circle distance, kilometres. */
  distanceKm: number;
}

/** The radius a sounding is still the pinned point's column within. */
export const SOUNDING_RADIUS_KM = 150;
/** The radius an airport's observation still describes the pinned point. */
export const AIRPORT_RADIUS_KM = 40;

/** Mean Earth radius, km — the sphere every distance here is measured on.
 * A spherical distance is a few tenths of a percent off the ellipsoid's,
 * which is nothing against a 40 km cut-off. */
const EARTH_RADIUS_KM = 6371.0088;

const RADIANS = Math.PI / 180;

/** Great-circle distance between two points in degrees, in kilometres.
 *
 * The longitude difference is taken modulo 360 into ±180 first, so a pin at
 * 179.9°E and a station at 179.9°W are 22 km apart rather than most of the
 * way around the world. */
export function haversineKm(
  longitudeA: number,
  latitudeA: number,
  longitudeB: number,
  latitudeB: number,
): number {
  const dLat = (latitudeB - latitudeA) * RADIANS;
  const dLon = (((longitudeB - longitudeA + 540) % 360) - 180) * RADIANS;
  const latA = latitudeA * RADIANS;
  const latB = latitudeB * RADIANS;
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(latA) * Math.cos(latB) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** The nearest of a list of positioned things inside `maxKm`, or null.
 *
 * Linear over the index — five thousand airports is a few hundred
 * microseconds, once per pin — and deliberately so: a spatial index would
 * have to be rebuilt every time a round replaces the index it was built
 * from, which is every ten minutes. */
function nearest<S extends { lat: number; lon: number }>(
  stations: readonly S[],
  longitude: number,
  latitude: number,
  maxKm: number,
): NearestStation<S> | null {
  let best: NearestStation<S> | null = null;
  for (const station of stations) {
    const distanceKm = haversineKm(longitude, latitude, station.lon, station.lat);
    if (distanceKm > maxKm) continue;
    if (best === null || distanceKm < best.distanceKm) best = { station, distanceKm };
  }
  return best;
}

/** The sounding station whose ascent the pinned point falls closest to, or
 * null when none is inside `maxKm`. */
export function nearestSounding(
  index: SoundingIndex,
  longitude: number,
  latitude: number,
  maxKm: number = SOUNDING_RADIUS_KM,
): NearestStation<SoundingStationEntry> | null {
  return nearest(index.stations, longitude, latitude, maxKm);
}

/** The airport whose observations describe the pinned point, or null when
 * none is inside `maxKm`. */
export function nearestAirport(
  index: AirportIndex,
  longitude: number,
  latitude: number,
  maxKm: number = AIRPORT_RADIUS_KM,
): NearestStation<AirportStation> | null {
  return nearest(index.stations, longitude, latitude, maxKm);
}
