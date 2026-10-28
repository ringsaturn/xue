/** Where the static storm pages live (`web/tooling/stormpages.ts` writes
 * them): English at the root, four languages under a prefix, the blog's
 * layout. Shared by the build and by the pages that link to them, so an
 * address is spelled in one place. A locale without pages of its own (the
 * site's other six) is sent to the English ones. */

export const PAGE_PREFIXES: Readonly<Record<string, string>> = {
  en: "",
  zh: "/zh",
  "zh-Hant": "/zh-hant",
  ja: "/ja",
  ko: "/ko",
};

export function pagePrefix(locale: string): string {
  return PAGE_PREFIXES[locale] ?? "";
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
