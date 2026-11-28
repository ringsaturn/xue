/** Where the site's pages are, in each language.
 *
 * The language is the path's first segment: English at the root, every
 * other locale under its prefix (`/ja/hrrr?type=wind`, `/zh/showcase`,
 * `/de/showcase/<id>#map=…`). Every page the shell renders — the home, a
 * dataset's page, the case list, a case, the comparison, the run metrics,
 * the storm map, the workbench and the API console — has one copy per locale, written by
 * the build (`web/tooling/sitepages.ts`). An unprefixed page doubles as
 * the language chooser (`LANGUAGE_CHOOSER`): a reader whose stored or
 * browser language is another one is sent to its prefix; a prefixed page
 * never moves.
 *
 * A link from before names the language as `?lang=` and a case as
 * `/?case=<id>`; `legacyLocaleLocation` says where such a link belongs, and
 * the shell applies it with `replaceState` on boot (`i18n.ts`).
 *
 * Nothing here imports at run time: `i18n.ts` and the build read it without
 * the shell's graph. */

export type Locale = "zh" | "zh-Hant" | "en" | "ja" | "ko" | "de" | "fr" | "es" | "pt" | "tr" | "ru" | "ne" | "ar";

/** Each locale's path prefix, in the picker's order: lowercase, the storm
 * pages' spelling (`zh-hant`); English has none. */
export const LOCALE_PREFIXES: Readonly<Record<Locale, string>> = {
  zh: "/zh",
  "zh-Hant": "/zh-hant",
  en: "",
  ja: "/ja",
  ko: "/ko",
  de: "/de",
  fr: "/fr",
  es: "/es",
  pt: "/pt",
  tr: "/tr",
  ru: "/ru",
  ne: "/ne",
  ar: "/ar",
};

export const LOCALE_CODES = Object.keys(LOCALE_PREFIXES) as Locale[];

/** Chinese is the one language here written in two scripts, and a tag names
 * the script either directly (`zh-Hant`) or through a region. Taiwan, Hong
 * Kong and Macau write traditional; the mainland and Singapore simplified. */
const TRADITIONAL_REGIONS = new Set(["tw", "hk", "mo"]);

/** Map a BCP-47 tag onto one of the thirteen, or null. Case-insensitive.
 * `LANGUAGE_CHOOSER` spells the same rules out in plain script. */
export function normalizeLocale(value: string | null | undefined): Locale | null {
  if (!value) return null;
  const parts = value.trim().toLowerCase().split(/[-_]/).filter(Boolean);
  const language = parts[0];
  if (!language) return null;
  if (language === "zh") {
    // The script subtag wins where there is one; otherwise the region says
    // which script the reader expects, and a bare `zh` means simplified.
    if (parts.includes("hant")) return "zh-Hant";
    if (parts.includes("hans")) return "zh";
    return parts.some((part) => TRADITIONAL_REGIONS.has(part)) ? "zh-Hant" : "zh";
  }
  // Every other language here has one script, so the region is dropped:
  // pt-BR and pt-PT are both `pt`, en-GB is `en`.
  return Object.hasOwn(LOCALE_PREFIXES, language) ? (language as Locale) : null;
}

const SLUG_LOCALES: ReadonlyMap<string, Locale> = new Map(
  LOCALE_CODES.filter((code) => code !== "en").map((code) => [LOCALE_PREFIXES[code].slice(1), code]),
);

/** A path split into its language and the page within it: `/ja/hrrr` is
 * Japanese `/hrrr`, `/ja/` (and `/ja`) Japanese `/`, `/hrrr` English
 * `/hrrr`. `prefixed` says whether the path named the language. */
export function splitLocalePath(pathname: string): { locale: Locale; path: string; prefixed: boolean } {
  const match = /^\/([a-z]+(?:-[a-z]+)?)(\/.*)?$/.exec(pathname);
  const locale = match ? SLUG_LOCALES.get(match[1]!) : undefined;
  if (!match || locale === undefined) return { locale: "en", path: pathname, prefixed: false };
  return { locale, path: match[2] && match[2] !== "" ? match[2] : "/", prefixed: true };
}

/** The address of `path` (root-relative, `/` or `/<page>`, a query or a
 * fragment allowed) in `locale`: `/ja/` for the Japanese home, `/ja/hrrr`
 * for a dataset's page, `path` itself in English. */
export function localizedPath(locale: Locale, path: string): string {
  const prefix = LOCALE_PREFIXES[locale];
  if (prefix === "") return path;
  return `${prefix}${path}`;
}

/** The same page in another language: `/ja/hrrr` → `/de/hrrr`. */
export function relocalizePath(pathname: string, locale: Locale): string {
  return localizedPath(locale, splitLocalePath(pathname).path);
}

/** The shell's pages besides the viewer's (`/`, `/<dataset>`, a case),
 * each one segment deep. */
export const SHELL_PAGES: readonly string[] = ["/showcase", "/compare", "/metrics", "/typhoon", "/workbench", "/api"];

/** The case list; a case is a page under it. */
export const SHOWCASE_PATH = "/showcase";

const CASE_ID = /^[a-z0-9-]+$/;

/** The path a case is at: `/showcase/<id>`. */
export function casePathname(id: string): string {
  return `${SHOWCASE_PATH}/${id}`;
}

/** The case a path within a locale (`splitLocalePath`'s `path`) names, or
 * null. */
export function caseFromPath(path: string): string | null {
  const match = /^\/showcase\/([^/]+)$/.exec(path);
  return match && CASE_ID.test(match[1]!) ? match[1]! : null;
}

/** Whether a path within a locale is one of the shell's pages, so a link
 * to it follows the reader's language: the home, the shell's other pages,
 * a case, or a dataset in `datasetIds`. The storm and airport pages are
 * not: they come in five languages of their own (`tc/pages.ts`). */
export function isShellPath(path: string, datasetIds: ReadonlySet<string>): boolean {
  if (path === "/" || SHELL_PAGES.includes(path) || caseFromPath(path) !== null) return true;
  const segment = /^\/([a-z0-9]+)$/.exec(path)?.[1];
  return segment !== undefined && datasetIds.has(segment);
}

/** True for the home's own addresses within a locale: `/` and the
 * `index.html` Pages redirects to it. */
export function isHomePath(path: string): boolean {
  return path === "/" || path === "/index.html";
}

/** Where a link naming its language as `?lang=` or its case as
 * `/?case=<id>` belongs, as a root-relative path and query, or null when it
 * is fine where it is.
 *
 * `?lang=<tag>` moves the page under that locale's prefix (English: none)
 * and leaves the query without it; a tag that names none of the thirteen is
 * dropped and the page stays in the language of its path. `?case=<id>` on
 * the home is the case's page, `/<prefix>/showcase/<id>`. Every other
 * parameter is kept in order; the fragment is the browser's to keep. */
export function legacyLocaleLocation(pathname: string, search: string): string | null {
  const params = new URLSearchParams(search);
  const lang = params.get("lang");
  const caseId = params.get("case");
  const split = splitLocalePath(pathname);
  const movesCase = caseId !== null && isHomePath(split.path);
  if (lang === null && !movesCase) return null;
  const locale = lang === null ? split.locale : (normalizeLocale(lang) ?? split.locale);
  params.delete("lang");
  let path = split.path;
  if (movesCase) {
    params.delete("case");
    if (CASE_ID.test(caseId!)) path = casePathname(caseId!);
  }
  const rest = params.toString();
  return `${localizedPath(locale, path)}${rest === "" ? "" : `?${rest}`}`;
}

/** The language chooser on an unprefixed (English) page, as an inline
 * script for the head: before anything loads, a reader whose stored choice
 * (`xue-locale`) or browser language is another of the thirteen is sent to
 * that locale's copy of the same page, the query and fragment kept. A
 * crawler stays (its own `User-Agent`, and no storage), as does a link
 * still carrying `?lang=`, which the mapping above places, and a page
 * already under a prefix (the dev server serves every language from the
 * source page). The tag rules are `normalizeLocale`'s. */
export const LANGUAGE_CHOOSER = `(function () {
  try {
    if (/bot|crawl|spider|slurp/i.test(navigator.userAgent)) return;
    if (/[?&]lang=/.test(location.search)) return;
    var prefixes = ${JSON.stringify(Object.fromEntries(LOCALE_CODES.map((code) => [code, LOCALE_PREFIXES[code]])))};
    var first = "/" + location.pathname.split("/")[1];
    for (var code in prefixes) if (prefixes[code] === first) return;
    var norm = function (tag) {
      if (!tag) return null;
      var parts = String(tag).trim().toLowerCase().split(/[-_]/).filter(Boolean);
      if (!parts.length) return null;
      if (parts[0] === "zh") {
        if (parts.indexOf("hant") >= 0) return "zh-Hant";
        if (parts.indexOf("hans") >= 0) return "zh";
        return parts.some(function (part) { return part === "tw" || part === "hk" || part === "mo"; }) ? "zh-Hant" : "zh";
      }
      return Object.prototype.hasOwnProperty.call(prefixes, parts[0]) ? parts[0] : null;
    };
    var wanted = null;
    try { wanted = norm(localStorage.getItem("xue-locale")); } catch (error) {}
    if (!wanted) {
      var tags = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
      for (var i = 0; i < tags.length && !wanted; i += 1) wanted = norm(tags[i]);
    }
    if (wanted && prefixes[wanted]) location.replace(prefixes[wanted] + location.pathname + location.search + location.hash);
  } catch (error) {
    // Stay on the English page.
  }
})();`;
