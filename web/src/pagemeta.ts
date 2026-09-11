/** The document's own description of itself, kept true after the shell
 * has decided what it is showing.
 *
 * The static markup carries the live viewer's English metadata as its pre-JS
 * fallback — what a social scraper reads. Once main.ts knows the view, the
 * head is rewritten from here: a case is its own page (`/?case=<id>`, its
 * title and summary), every live view is the one page at `/`, and the ten
 * `?lang=` renderings of either are declared as hreflang alternates that each
 * name themselves canonical. Search engines render the module bundle, so
 * this is what they index; the pre-JS copy is for everything that does not.
 *
 * View state that is not content — `?model=`, `?type=`, `?res=` — folds into
 * the canonical URL on purpose: it is the same application, and ranking one
 * page beats splitting it ten ways. */

import { LOCALES, localeHtmlLang, normalizeLocale } from "./i18n";
import type { Locale } from "./i18n";
import { SITE_NAME, SITE_ORIGIN } from "./site";

export interface PageMeta {
  /** Root-relative path with the query that defines the *content* — `/`
   * for the live viewer, `/?case=<id>` for a case, `/showcase.html`. */
  path: string;
  /** Full document title; `· Xue` is appended here so a caller composes
   * only the part that names the view. */
  title?: string;
  description?: string;
}

/** The absolute URL of a page rendered in one language, or in the
 * browser's own when `lang` is null. */
export function pageUrl(path: string, lang: Locale | null): string {
  const url = new URL(path, SITE_ORIGIN);
  if (lang) url.searchParams.set("lang", lang);
  return url.href;
}

/** The hreflang set for a path: one entry per locale plus the x-default. */
export function alternateUrls(path: string): { hreflang: string; href: string }[] {
  return [
    ...LOCALES.map(({ code }) => ({ hreflang: localeHtmlLang(code), href: pageUrl(path, code) })),
    { hreflang: "x-default", href: pageUrl(path, null) },
  ];
}

/** The canonical URL of a page as it was requested: a rendering pinned to a
 * language by `?lang=` is canonical to itself, so every alternate the
 * hreflang set names points back at its own entry. */
export function canonicalUrl(path: string, search: string): string {
  return pageUrl(path, normalizeLocale(new URLSearchParams(search).get("lang")));
}

function headLink(selector: string, create: () => HTMLLinkElement): HTMLLinkElement {
  const existing = document.head.querySelector<HTMLLinkElement>(selector);
  if (existing) return existing;
  const link = create();
  document.head.appendChild(link);
  return link;
}

function setMetaContent(selector: string, content: string): void {
  for (const element of document.head.querySelectorAll<HTMLMetaElement>(selector)) {
    element.setAttribute("content", content);
  }
}

/** Rewrite the head for the view on screen. Elements the markup ships are
 * updated in place; missing ones are added, so a page need not list the
 * whole hreflang set to get it. */
export function applyPageMeta(meta: PageMeta, search: string = window.location.search): void {
  const canonical = canonicalUrl(meta.path, search);
  headLink('link[rel="canonical"]', () => {
    const link = document.createElement("link");
    link.rel = "canonical";
    return link;
  }).href = canonical;
  setMetaContent('meta[property="og:url"]', canonical);

  for (const { hreflang, href } of alternateUrls(meta.path)) {
    headLink(`link[rel="alternate"][hreflang="${hreflang}"]`, () => {
      const link = document.createElement("link");
      link.rel = "alternate";
      link.hreflang = hreflang;
      return link;
    }).href = href;
  }

  if (meta.title !== undefined) {
    const title = `${meta.title} · ${SITE_NAME}`;
    document.title = title;
    setMetaContent('meta[property="og:title"], meta[name="twitter:title"]', title);
  }
  if (meta.description !== undefined) {
    setMetaContent(
      'meta[name="description"], meta[property="og:description"], meta[name="twitter:description"]',
      meta.description,
    );
  }
}
