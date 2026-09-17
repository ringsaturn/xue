/**
 * The geostationary mosaic: several disks shown as one picture.
 *
 * Every geostationary imager sits over the equator, so "the satellite that
 * sees a point least obliquely" — the smallest zenith angle — is simply the
 * one whose sub-satellite longitude is nearest, and the mosaic is a
 * partition of longitude at the midpoints between neighbouring
 * sub-satellite points. Nothing is computed from the data: each member's
 * published run is opened as it is and its layer clipped to its band
 * (`ForecastLayer.setBand`); the disk's own edge, code 0 in every satellite
 * bundle, ends the layer inside the band well before the band does.
 *
 * The members' windows start at different hours and run at different
 * cadences (ten minutes on the disks, an hour on Meteosat's licensed
 * cycle), so they are matched on the valid time of a frame, never on lead
 * seconds: a member shows its latest frame not later than the playhead,
 * and holds it for one of its own steps.
 */

/** A range of longitude, `width` degrees eastward from `start`, in
 * whichever copy of the world the longitude is spelled: a point is inside
 * when its eastward distance from `start`, wrapped into [0, 360), is at
 * most `width`. A width of 360 is the whole world. */
export interface LongitudeBand {
  start: number;
  width: number;
}

/** Wrap a longitude into [0, 360). */
export function wrapLongitude(longitude: number): number {
  const wrapped = longitude % 360;
  return wrapped < 0 ? wrapped + 360 : wrapped;
}

/** Whether a longitude, in any copy of the world, falls in the band. */
export function bandContains(band: LongitudeBand, longitude: number): boolean {
  if (band.width >= 360) return true;
  return wrapLongitude(longitude - band.start) <= band.width;
}

/** The band of each member, by id: from the midpoint with its western
 * neighbour to the midpoint with its eastern one, wrapping round the
 * globe. A lone member takes the whole world. */
export function mosaicBands<Id extends string>(
  members: readonly { id: Id; subLongitude: number }[],
): Map<Id, LongitudeBand> {
  const bands = new Map<Id, LongitudeBand>();
  if (members.length === 0) return bands;
  const sorted = [...members]
    .map((member) => ({ id: member.id, longitude: wrapLongitude(member.subLongitude) }))
    .sort((a, b) => a.longitude - b.longitude);
  if (sorted.length === 1) {
    bands.set(sorted[0]!.id, { start: wrapLongitude(sorted[0]!.longitude - 180), width: 360 });
    return bands;
  }
  for (const [index, member] of sorted.entries()) {
    const west = sorted[(index - 1 + sorted.length) % sorted.length]!.longitude;
    const east = sorted[(index + 1) % sorted.length]!.longitude;
    // Eastward gaps to each neighbour, wrapped, so the last member's eastern
    // neighbour is the first one a world away.
    const gapWest = wrapLongitude(member.longitude - west) || 360;
    const gapEast = wrapLongitude(east - member.longitude) || 360;
    const start = wrapLongitude(member.longitude - gapWest / 2);
    bands.set(member.id, { start, width: gapWest / 2 + gapEast / 2 });
  }
  return bands;
}

/** The step a member holds a frame for: the shortest gap between two of
 * its valid times, or `fallbackMs` for an axis of one frame. */
export function axisCadenceMs(validTimes: readonly number[], fallbackMs: number): number {
  let cadence = Number.POSITIVE_INFINITY;
  for (let index = 1; index < validTimes.length; index += 1) {
    const step = validTimes[index]! - validTimes[index - 1]!;
    if (step > 0 && step < cadence) cadence = step;
  }
  return Number.isFinite(cadence) ? cadence : fallbackMs;
}

/** How far apart a member's slot and the playhead's may be and still count
 * as the same moment: the GOES scans start twenty seconds into the slot
 * the files are keyed by, and nothing legitimate is minutes off. */
export const VALID_TIME_TOLERANCE_MS = 60 * 1000;

/** The member's frame for a playhead valid time: the latest frame not
 * later than the playhead (within the tolerance), provided the playhead
 * has not moved on by a whole step of the member's own axis — an hourly
 * frame stays up under a ten-minute playhead for its hour, a member whose
 * window has not reached the playhead shows nothing. `validTimes` ascend
 * and `offsets` are the same frames' keys on the member's axis; returns
 * the offset or null. */
export function offsetNotLater(
  validTimes: readonly number[],
  offsets: readonly number[],
  playhead: number,
  cadenceMs: number,
  toleranceMs: number = VALID_TIME_TOLERANCE_MS,
): number | null {
  let found = -1;
  for (let index = 0; index < validTimes.length; index += 1) {
    if (validTimes[index]! <= playhead + toleranceMs) found = index;
    else break;
  }
  if (found < 0) return null;
  if (playhead - validTimes[found]! >= cadenceMs + toleranceMs) return null;
  return offsets[found] ?? null;
}

/** Which present member drives the timeline: the one with the finest
 * cadence, then the newest window; returns its id or null when none. */
export function primaryMember<Id extends string>(
  members: readonly { id: Id; cadenceSeconds: number; runTime: number }[],
): Id | null {
  let best: { id: Id; cadenceSeconds: number; runTime: number } | null = null;
  for (const member of members) {
    if (
      best === null ||
      member.cadenceSeconds < best.cadenceSeconds ||
      (member.cadenceSeconds === best.cadenceSeconds && member.runTime > best.runTime)
    ) {
      best = member;
    }
  }
  return best?.id ?? null;
}
