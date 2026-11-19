/** The document's own description of itself, kept true after the shell
 * has decided what it is showing.
 *
 * Each language's copy of a page ships its own head (the build writes it,
 * `web/tooling/sitepages.ts`): what a social scraper and a crawler that
 * runs no script read. Once the shell knows the view, the head is
 * rewritten from here: a case is its own page (`/showcase/<id>`, its title
 * and summary), a live view is its dataset's page (`/hrrr`, the path the
 * shell writes, `urlstate.ts::livePathname`) or the home (`/`, on the
 * default dataset until another is chosen), and the twelve languages' copies
 * of any of them (`/ja/hrrr`, `sitepaths.ts`) are declared as hreflang
 * alternates that each name themselves canonical, the unprefixed English
 * copy doubling as the x-default.
 *
 * The dataset is content: each one is a proper noun a reader searches for,
 * with a domain, a cadence and fields of its own, so each has a page. View
 * state within a dataset — `?type=`, `?res=`, the camera — folds into its
 * canonical URL on purpose: it is the same page, and ranking one beats
 * splitting it forty ways. */

import { LOCALES, locale, localeHtmlLang, localeOgLocale } from "./i18n";
import type { Locale } from "./i18n";
import { localizedPath } from "./sitepaths";
import { SITE_NAME, SITE_ORIGIN } from "./site";

interface PageMeta {
  /** Root-relative path within a language, with the query that defines the
   * *content* — `/` or `/<id>` for the live viewer, `/showcase/<id>` for a
   * case, `/showcase`. */
  path: string;
  /** Full document title; `· Xue` is appended here so a caller composes
   * only the part that names the view. */
  title?: string;
  description?: string;
  /** Absolute URL of another page that holds this content, when there is
   * one: the head names it canonical and drops this page's hreflang set,
   * whose alternates would each claim to be canonical themselves. */
  canonical?: string;
}

/** The absolute URL of a page in one language; null is the x-default, the
 * unprefixed copy (which is the English one). */
export function pageUrl(path: string, lang: Locale | null): string {
  return new URL(localizedPath(lang ?? "en", path), SITE_ORIGIN).href;
}

/** The hreflang set for a path: one entry per locale plus the x-default. */
export function alternateUrls(path: string): { hreflang: string; href: string }[] {
  return [
    ...LOCALES.map(({ code }) => ({ hreflang: localeHtmlLang(code), href: pageUrl(path, code) })),
    { hreflang: "x-default", href: pageUrl(path, null) },
  ];
}

/** The canonical URL of a page in a language: each language's copy is
 * canonical to itself, so every alternate the hreflang set names points
 * back at its own entry. */
export function canonicalUrl(path: string, lang: Locale): string {
  return pageUrl(path, lang);
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

/** Rewrite the head for the view on screen, in the page's language (the
 * live `locale` unless `lang` says). Elements the markup ships are updated
 * in place; missing ones are added, so a page need not list the whole
 * hreflang set to get it. */
export function applyPageMeta(meta: PageMeta, lang: Locale = locale): void {
  const canonical = meta.canonical ?? canonicalUrl(meta.path, lang);
  setMetaContent('meta[property="og:locale"]', localeOgLocale(lang));
  headLink('link[rel="canonical"]', () => {
    const link = document.createElement("link");
    link.rel = "canonical";
    return link;
  }).href = canonical;
  setMetaContent('meta[property="og:url"]', canonical);

  if (meta.canonical !== undefined) {
    for (const link of document.head.querySelectorAll('link[rel="alternate"][hreflang]')) link.remove();
  }
  for (const { hreflang, href } of meta.canonical === undefined ? alternateUrls(meta.path) : []) {
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
