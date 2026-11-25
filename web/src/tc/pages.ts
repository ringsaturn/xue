/** Where the static storm pages live (`web/tooling/stormpages.ts` writes
 * them): English at the root, four languages under their prefix, the blog's
 * layout. Shared by the build and by the pages that link to them, so an
 * address is spelled in one place.
 *
 * The prefixes are the site's (`sitepaths.ts`, every locale has one), but
 * the storm and airport pages come in five languages only
 * (`STORM_PAGE_LOCALES`): a locale without pages of its own (the site's
 * other seven) is sent to the English ones. */

import { LOCALE_PREFIXES } from "../sitepaths";

/** Every locale's path prefix, the site's. */
export const PAGE_PREFIXES: Readonly<Record<string, string>> = LOCALE_PREFIXES;

/** The languages the storm and airport pages are written in. */
export const STORM_PAGE_LOCALES: readonly string[] = ["en", "zh", "zh-Hant", "ja", "ko"];

/** The prefix of a storm or airport page in `locale`: its own, or the
 * English pages' for a locale they are not written in. */
export function pagePrefix(locale: string): string {
  return STORM_PAGE_LOCALES.includes(locale) ? (PAGE_PREFIXES[locale] ?? "") : "";
}

/** Whether a path within a locale is a storm or airport page's. */
export function isStaticPagePath(path: string): boolean {
  return /^\/(tc|airports)\//.test(path);
}

/** `choi-wan-wp262026`: the name (names are reused, so the ATCF id after
 * it). */
export function stormSlug(storm: { id: string; name: string | null }): string {
  const name = (storm.name ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `${name ? `${name}-` : ""}${storm.id.toLowerCase()}`;
}

/** A page's root-relative address as Pages serves it (no `.html`). */
export function archivePagePath(locale: string): string {
  return `${pagePrefix(locale)}/tc/`;
}

export function seasonPagePath(locale: string, season: number): string {
  return `${pagePrefix(locale)}/tc/${season}/`;
}

export function stormPagePath(locale: string, season: number, storm: { id: string; name: string | null }): string {
  return `${pagePrefix(locale)}/tc/${season}/${stormSlug(storm)}`;
}
