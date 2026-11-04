/** Which fields the rail writes a generic tile for, and how those tiles tell
 * each other apart. A dataset the shell has no written tiles for still names
 * its fields (a case-only dataset's catalog row), so those are reachable from
 * the rail like any core tile; the rest only while on screen. */

/** The generic tiles a run needs, in rail order: its core fields no written
 * tile stands for, then the field on screen if it is neither. */
export function genericRailTileIds(
  core: readonly string[],
  published: (id: string) => boolean,
  fill: string | null,
  hasWrittenTile: (id: string) => boolean,
): string[] {
  const ids = core.filter((id) => published(id) && !hasWrittenTile(id));
  if (fill !== null && !ids.includes(fill) && !hasWrittenTile(fill)) ids.push(fill);
  return ids;
}

/** A generic tile's glyph: the id's initial, joined by the first letter that
 * differs from any sibling sharing that initial, since the tiles carry the
 * same icon and the glyph is all that separates them at rest. */
export function genericRailGlyph(id: string, siblings: readonly string[]): string {
  const rivals = siblings.filter((other) => other !== id && other[0] === id[0]);
  if (rivals.length === 0) return id.slice(0, 1).toUpperCase();
  for (let i = 1; i < id.length; i += 1) {
    if (rivals.every((other) => other[i] !== id[i])) return `${id[0]}${id[i]}`.toUpperCase();
  }
  return id.slice(0, 2).toUpperCase();
}
