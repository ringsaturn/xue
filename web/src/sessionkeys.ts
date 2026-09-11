/**
 * Keys for everything the app holds per decoded plane.
 *
 * A variable's `numericId` is a **file-local** handle: the encoder numbers a
 * bundle's variables 1..n, so the temperature fill and the pressure lines
 * drawn over it both carry variable 1, and the 10 m wind's u and v are 1 and
 * 2 in *their* file. Nothing outside a single `.xue` file may be keyed by
 * that number alone.
 *
 * So every key that leaves a session carries the session too. `SessionKey` is
 * a small monotonic number main.ts hands each open session; it is never
 * derived from the bundle id, so a session reloaded under the same name does
 * not inherit the previous one's cache entries.
 *
 * Kept in its own module because the frame cache, the eviction pass, the
 * prefetch window and the probe series all have to agree on the shape, and
 * because a collision between two open bundles is exactly the kind of bug
 * that renders plausibly and wrongly.
 */

/** Anything carrying a session's cache identity (a `VariableSession`). */
export interface SessionKeyed {
  key: number;
}

/** Anything carrying a file-local variable number (a `BundleVariable`). */
export interface NumberedVariable {
  numericId: number;
}

const SEPARATOR = ":";

/** The key one of a session's variables is filed under, across every frame:
 * the probe series' key, and the prefix of the frame cache's. */
export function variableKey(session: SessionKeyed, variable: NumberedVariable): string {
  return `${session.key}${SEPARATOR}${variable.numericId}`;
}

/** The frame cache's key: one session's variable at one frame offset. */
export function frameCacheKey(session: SessionKeyed, variable: NumberedVariable, frameOffset: number): string {
  return `${variableKey(session, variable)}${SEPARATOR}${frameOffset}`;
}

/** The three parts of a frame cache key, or null when the string is not one.
 * Reading a key back is only ever a convenience — the session that owns a
 * cached plane is stored beside it, never re-derived from its key. */
export function parseFrameCacheKey(
  key: string,
): { sessionKey: number; numericId: number; frameOffset: number } | null {
  const parts = key.split(SEPARATOR);
  if (parts.length !== 3) return null;
  const [sessionKey, numericId, frameOffset] = parts.map(Number);
  if (![sessionKey, numericId, frameOffset].every((value) => Number.isFinite(value))) return null;
  return { sessionKey: sessionKey!, numericId: numericId!, frameOffset: frameOffset! };
}
